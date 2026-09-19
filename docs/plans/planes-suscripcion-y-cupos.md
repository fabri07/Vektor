# Backend de planes, suscripción y cupos (Etapa 2)

Estado: **implementado y probado, sin desplegar.** Sigue la política de planes
acordada (ver el Artifact "Planes y Precios — Véktor" compartido con el
socio) y la revisión técnica que la precisó. Etapa 3 (Mercado Pago) queda
fuera — ver "Fuera de alcance" al final.

## Qué existe ahora

### Catálogo de planes — `plan_definitions`
Tabla nueva (migración `20260918_0001`), sembrada con 4 filas: `FREE` (legado,
`is_offered=false`, cupos "sin límite configurado" — preserva el
comportamiento previo) y `esencial`/`control`/`direccion` con los cupos y
precios de la política. Editar esta tabla **nunca** cambia una `Subscription`
ya otorgada: sus condiciones quedan copiadas (`granted_*`) al activar/renovar,
no resueltas en vivo contra el catálogo.

### Estados de `Subscription`
`status` es un `VARCHAR` + `CHECK` cerrado: `TRIAL` / `ACTIVE` / `GRACE` /
`READ_ONLY` / `CANCELLED` (`app/domain/subscription.py::SubscriptionStatus`).
Nuevas columnas: `trial_ends_at`, `grace_ends_at`, `cancel_at_period_end`,
`granted_ia_queries_per_month`, `granted_imports_per_month`,
`granted_photo_pdf_reads_per_month` — todas timezone-aware (`current_period_
start/end` migraron de `DATE` a `TIMESTAMPTZ`). Índice único parcial
`uq_subscriptions_one_live_per_tenant` (solo Postgres): un tenant no puede
tener dos suscripciones no-canceladas a la vez.

`effective_access()` (mismo módulo) resuelve estado + cupos + período vigente
de una `Subscription` en un momento dado, **por fecha, no por el string de
`status`**: una prueba o una gracia vencidas bloquean aunque nadie haya
corrido el job que las pasa a `READ_ONLY`. `GRACE` conserva el MISMO período
que ya corría — nunca se acredita un cupo nuevo sin confirmar la renovación.

### Cupos — reserva/confirmación/liberación atómica
Dos tablas nuevas: `subscription_quota_usage` (contador `used`+`reserved` por
tenant/recurso/período, con `limit_snapshot` propio) y
`subscription_quota_reservations` (una fila por operación, identificada por
`operation_id` — elegido por quien llama, para que un reintento nunca reserve
una unidad de más). `app/application/services/subscription_service.py`:

- `reserve(session, subscription, resource, operation_id)` — atómico
  (`INSERT ... ON CONFLICT ... WHERE ... RETURNING`, mismo espíritu que
  `operation_identity_service`), reserva ANTES de ejecutar. Tira
  `SubscriptionAccessDenied` (estado no habilita) o `QuotaExceeded` (sin
  cupo).
- `commit(...)` / `release(...)` — confirman o liberan, idempotentes (un
  reintento sobre una reserva ya resuelta es no-op).
- `enforce_active_subscription(subscription)` — la capa transversal: bloquea
  sin mirar ningún cupo.
- `enforce_seat_limit(...)` — `SELECT ... FOR UPDATE` sobre la fila de
  `Subscription` antes de contar usuarios activos, para que dos altas
  simultáneas no superen `seats_included` (**nota**: la función existe;
  todavía no está enganchada en `POST /users` — ver "Fuera de alcance").

**Probado bajo concurrencia real** (`app/tests/integration/test_subscription_
quota_pg.py`, contra Postgres, no SQLite): con 1 solo cupo, dos operaciones
DISTINTAS peleando la última unidad — una gana, la otra ve `QuotaExceeded`,
nunca las dos ni ninguna. Reintentar la MISMA operación no duplica.

### Aprobación de solicitudes → `TRIAL`, nunca `ACTIVE`
`provision_tenant()` acepta `plan_code` opcional: `None` (default, camino de
`AuthService.register`/demo) preserva el comportamiento `FREE`/`ACTIVE` de
siempre; con `plan_code`, la cuenta nace en `TRIAL` con `trial_ends_at = +14
días` — nunca copia `requested_plan` directo. `AccessRequestService.approve()`
ahora exige `assigned_plan_code: AssignablePlan` (mismo criterio que
`assigned_vertical` vs `requested_vertical`): el dueño confirma explícitamente
con qué plan arranca la prueba, en CADA aprobación — nunca se infiere de
`requested_plan` (ni siquiera para los históricos `free`/`premium`).
`scripts/access_requests.py approve` ahora exige `--plan`.

### `/chat` y `/chat/stream` — cupo de IA real
Reserva 1 `ia_query` ANTES de despachar al orquestador (`app/api/v1/agent.py`,
helpers `_reservar_consulta_ia`/`_resolver_consulta_ia`); confirma si el turno
sale bien, libera si sale mal (timeout, error del proveedor, excepción). El
límite diario global de 50 msj/día en Redis **sigue vigente sin cambios** — es
un anti-abuso aparte; los cupos por plan no dependen de que Redis esté vivo.
Probado end-to-end vía HTTP real (`test_endpoints.py`): bloqueo por
`READ_ONLY` (402), cupo agotado (429), y confirmación tras un turno exitoso.

### Gate transversal de escritura — `require_active_subscription`
Dependency nueva en `app/api/v1/deps.py`, mismo patrón que
`ensure_tenant_not_under_maintenance`. Bloquea con 402
(`SUBSCRIPTION_READ_ONLY`) las escrituras de negocio cuando la suscripción no
habilita — **nunca** la corrección de datos ya cargados (ver "Qué no debe
limitarse" en la política): enganchado en `POST /sales`, `POST /sales/bulk`,
`POST /sales/manual-batch` y `POST /expenses` (crear), explícitamente NO en
los `PATCH`/`DELETE` de esos mismos recursos.

### Activación manual — `scripts/subscriptions.py`
Mientras no exista Mercado Pago, el dueño activa un plan pago (cobro por
transferencia/link manual) desde acá — nunca a mano en la base:

```
show <tenant_id|email>
activate <tenant_id|email> --plan control --price-usd 27 --price-ars 41500 \
         --reference "MP-123456" [--months 1] [--apply]
set-status <tenant_id|email> --status READ_ONLY|GRACE|CANCELLED|ACTIVE|TRIAL [--apply]
expire-due [--apply]
```

`activate` es idempotente por `--reference`: repetir el mismo comando con la
misma referencia de pago no vuelve a extender el período. Todo cambio queda
auditado en `decision_audit_log` (`MANUAL_PLAN_ACTIVATION`,
`MANUAL_SUBSCRIPTION_STATUS_CHANGE`, `SUBSCRIPTION_EXPIRE_DUE`) en la misma
transacción. `expire-due` persiste vencimientos por fecha para que
`SELECT status` refleje la realidad — el enforcement real nunca depende de
que este comando haya corrido.

### `/auth/me`
`TenantRepository.get_active_subscription` (solo reconocía `ACTIVE`) se
reemplazó por `get_current_subscription` (cualquier estado no-`CANCELLED`).
`SubscriptionInMeResponse` expone `trial_ends_at`/`current_period_end`.

## Bloque A — bordes del acceso y de las reservas (cerrado)

Revisión posterior contra el código (`suscripciones-pendientes-y-cobro.md`,
bloque A). Corrige lo descrito arriba; donde contradiga, vale esto.

- **`ACTIVE` vence por fecha.** Pasado `current_period_end` corre la gracia
  con el MISMO período (y su saldo) durante `GRACE_DAYS`; después bloquea con
  `periodo_vencido`, aunque `status` siga diciendo `ACTIVE` y nadie haya
  corrido `expire-due`. Un plan pago `ACTIVE` sin período bloquea
  (`activa_sin_periodo`): faltar el dato nunca reabre el acceso. Todos los
  intervalos son `[inicio, fin)`.
- **FREE legado no vence** (`is_legacy_free`). El seed de demo lo acuña con
  período a 30 días; esas fechas nunca fueron un vencimiento. `expire-due`
  tenía el mismo agujero —pasaba a `GRACE` cualquier `ACTIVE` vencido, FREE
  incluido— y ahora lo exceptúa igual.
- **Cancelar ya no saca a la cuenta del control.** `get_current_subscription`
  excluía `CANCELLED` → `None` → gate y cupo seguían de largo: cancelar daba
  IA ilimitada, y la rama `CANCELLED` de `effective_access()` era
  inalcanzable. Ahora devuelve la viva o, si no hay, la última cancelada.
- **Sin ninguna `Subscription` → 503 `SUBSCRIPTION_UNAVAILABLE`** + error en
  el log (gate de escritura y chat). Es un dato roto nuestro, no un plan
  gratis ni una deuda del usuario (por eso no es 402).
  **Antes de desplegar:** `scripts/subscriptions.py diagnose` (solo lectura)
  contra producción; lista los tenants sin suscripción y los planes pagos
  `ACTIVE` sin período. Si devuelve alguno, repararlo primero.
- **`reserve()` devuelve `ReserveOutcome`** (`NEW` / `IN_PROGRESS` /
  `ALREADY_COMMITTED` / `ALREADY_RELEASED`). Solo `NEW` autoriza a ejecutar;
  el chat responde 409 `OPERATION_ALREADY_SUBMITTED` en los otros tres. La
  misma clave con otra cantidad de unidades tira `ReservationMismatch`.
  `commit()`/`release()` devuelven si ESA llamada resolvió la reserva.
- **`autocommit=False`** en `reserve/commit/release`: no publican lo
  pendiente de una sesión compartida. Es la primitiva que necesita el bloque
  C (importaciones); el chat sigue con transacción corta propia.
- **`enforce_seat_limit` recibe CÓMO contar, no un número**: bloquear →
  contar → crear. Las plazas salen del acceso efectivo (una sola durante la
  prueba) y una suscripción que no habilita escribir no suma usuarios.
  Probado con dos altas simultáneas contra Postgres: sin el `FOR UPDATE`
  entran las dos. **Sigue sin enganchar en `POST /users`** (bloque B).

Diferido: la clave estable para deduplicar reintentos HTTP del chat pasa al
bloque E — necesita que el cliente la mande.

## Verificación

- Migración probada con round-trip completo (`upgrade`/`downgrade`/`upgrade`)
  contra Postgres real.
- Suite completa del backend: **5085 passed, 175 skipped, 0 failed** (sin
  regresiones en el resto del sistema).
- `ruff check .` y `mypy app --ignore-missing-imports` limpios.
- Concurrencia real verificada contra Postgres (no simulada en SQLite).

## Fuera de alcance de esta entrega

- **Mercado Pago** (Etapa 3) — la activación sigue siendo manual.
- **Job periódico de vencimiento**: `expire-due` es manual/cron, no un Celery
  beat nuevo. El enforcement en caliente no depende de esto.
- **Cobertura exhaustiva del gate transversal**: `require_active_subscription`
  solo está en ventas y gastos (creación). Todavía faltan: stock/movimientos
  de inventario, cierres de caja, altas de proveedores/clientes, remitos,
  automatizaciones, custom fields. Mismo patrón, mismo import — es trabajo
  mecánico pendiente, no una decisión de diseño abierta.
- **`enforce_seat_limit` sin enganchar**: existe y está probado, pero
  `POST /users` todavía no lo llama.
- **Cupos de importación y lectura de foto/PDF sin enganchar**: el mecanismo
  de reserva es genérico (`QuotaResource.IMPORT`/`PHOTO_PDF_READ`), pero
  todavía no está conectado a `ingestion_import_service.confirm_file` (hay que
  coordinarlo con el lease existente, que hace su propio commit) ni a
  `remito_extraction_service.py`/`customer_extraction_service.py`.
- **Reconciliación de reservas huérfanas** (`reconcile-reservations`): no
  implementado — si un proceso muere con una reserva en `RESERVED`, hoy queda
  colgada hasta que alguien la resuelva a mano.
- **Frontend**: nada de UI todavía (pantalla de facturación, aviso de
  prueba/gracia/solo lectura).
- Límites de página/MB por archivo, prorrateo de upgrades, descuentos: siguen
  fuera, como ya establecía la política.
