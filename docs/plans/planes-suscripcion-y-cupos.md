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
  `gracia_vencida`, aunque `status` siga diciendo `ACTIVE` y nadie haya
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

## Bloque B — la misma política por API, agente y usuarios (cerrado)

- **Una sola autorización, usable fuera de FastAPI:**
  `subscription_service.assert_tenant_can_write(session, tenant_id, origen=)`.
  Una dependency no corre cuando el agente o un worker llaman a una función
  Python; `deps.require_active_subscription` quedó como su traducción a
  402/503 y nada más. Se llama UNA vez por operación: los subpasos de algo ya
  autorizado no vuelven a preguntar.
- **Clasificación por efecto, no por verbo HTTP.** Llevan gate las 20 rutas
  de ALTA (ventas, gastos, productos y categorías, cierres de caja,
  clientes/proveedores y su import, remitos, campos, automatizaciones,
  métricas de marketing) y las dos extracciones por foto/PDF — que llaman a
  Claude: en solo lectura eran gasto de IA sin respaldo (acá solo el estado;
  el CUPO de lectura es del bloque C). `PATCH`/`DELETE`, reactivar,
  toggle/undo de campos y `dismiss` de «Otros» quedan siempre abiertos.
  **Decisión de producto:** clasificar filas de «Otros» (`reclassify`,
  `resolve-purchase`, `bulk-import`) SÍ se bloquea — crea ventas/gastos
  nuevos aunque el archivo ya estuviera importado.
  Compuerta: `test_las_rutas_con_gate_son_exactamente_estas` compara el
  conjunto REAL de rutas con gate contra la lista esperada.
- **Agente: un solo embudo.** `execute_pending_action` verifica la
  suscripción — cubre botón, confirmación por texto (que corre antes de
  reservar IA y no pasa por ningún gate HTTP), grupo, auto-ejecución,
  automatizaciones y retry. Lista cerrada `WRITE_EXEMPT_ACTION_TYPES`
  (correcciones `UPDATE_PRODUCT`/`RECLASSIFY_EXPENSE`, los analíticos, el
  link de WhatsApp): **todo `ActionType` que no esté ahí nace bloqueado**, y
  `test_todo_action_type_esta_clasificado` obliga a decidir los nuevos. El
  rechazo llega al chat por `user_message`, como `InsufficientStockError`.
- **Plazas en `POST /users`:** bloquear → contar → crear en la transacción
  del request (`repo.save` hace flush; el commit es el del request). 409
  `SEAT_LIMIT_EXCEEDED` con `seats_included`/`active_users`; 402 si la
  suscripción no habilita. Las bajas siguen libres. No existe reactivación
  de usuarios, así que es el único camino de alta fuera del provisioning.
  **FREE legado no tiene tope** (su `seats_included=1` es un default de
  columna, no una condición comercial). `diagnose` lista los tenants pagos
  con más usuarios activos que plazas: conservan los que tienen.
- No hay workers que creen datos de negocio fuera de la ingesta (bloque C).

## Bloque C — consumos conectados y reservas conciliables (cerrado)

- **Importaciones: un solo punto de consumo.** El ejecutor en segundo plano
  llama al mismo `confirm_file` que la ruta síncrona, así que el cupo se
  consume ADENTRO de `confirm_file`, justo antes de `finalize_import_lease`,
  con `consume_with_data()` (sin commit propio): el consumo entra o se
  revierte JUNTO con los datos. Va al final para no retener la fila del
  contador durante todo el import; el 429 rápido lo da un chequeo temprano y
  NO vinculante (`assert_quota_available`) antes del lease. Perder la última
  unidad contra otra importación → rollback total + lease compensado + 429
  (handler global en `main.py`: la excepción tiene que llegar cruda al
  `except` que compensa, no se puede traducir en el lugar).
- **En segundo plano:** `POST /files/{id}/imports` autoriza y RESERVA en la
  misma transacción que el intento y su orden (clave
  `import-attempt:{attempt_id}`; rechazo → rollback explícito, no queda
  intento). El ejecutor confirma ESA reserva — no cobra otra ni re-mira la
  suscripción: lo ya autorizado termina aunque venza después. Un intento que
  cierra `FALLADO` libera su reserva en la transacción del cierre, **sólo si
  el cierre fue suyo** (si perdió el lease, otro ejecutor corre con ella).
  La clave viaja por `quota_operation_id`, que es una *dependency* y no un
  parámetro común a propósito: un `str | None` suelto sería query param, y
  mandar la clave de una importación ya cobrada haría gratis las siguientes.
- **No consumen:** el 422 previo al lease, el import vacío, el fallo técnico
  (rollback) y la **relectura** (pasa por `insert_confirmed_data`, no por
  `confirm_file`: es corrección). Un import con filas a «Otros» sí consume.
- **Lecturas foto/PDF** (`photo_pdf_read`, context manager): reserva sólo si
  el archivo va al modelo (`file_parsing.reads_with_ai`, mismo despacho que
  los extractores — una planilla no cuesta), confirma sólo si la lectura
  devolvió datos, libera si falló o vino vacía.
- **Gate también en** `POST /ingestion/upload`, `/confirm` e `/imports`.
- **Conciliación** (`subscription_reconciliation.py`,
  `scripts/subscriptions.py reconcile-reservations [--apply]`, job
  `jobs.reconcile_subscription_reservations` cada 30 min — Beat todavía no
  está desplegado, así que hoy es manual). **Nunca libera sólo por
  antigüedad**: import → manda el estado del intento (vivo = no tocar, tenga
  la edad que tenga); chat → la clave pasó a ser `chat:{trace_id}`, el mismo
  id que `decision_audit_log` guarda solo: fila con tokens = confirmar, sin
  fila y pasado el plazo = liberar (un request HTTP no tiene lease ni
  reintento: pasado el plazo no puede seguir vivo); lectura → mismo
  argumento; clave desconocida → ambigua, se conserva, se reporta y se
  resuelve a mano con `--reservation --action --reason`, auditado. Todo por
  el CAS de `RESERVED`, así que competir con el dueño real es no-op.
- Las importaciones síncronas no pueden colgarse: reservan y confirman en la
  transacción de los datos.

Fuera de este bloque: topes de páginas/MB por lectura (política pendiente), el
parseo de imágenes dentro del pipeline de ingesta (corre en el worker de
parseo, necesita su propio análisis), `reprocess` sin gate.

Sobre A6 (desconexión SSE): si el cliente corta, `CancelledError` no entra en
el `except Exception` del generador, así que la reserva NO se libera: queda en
`RESERVED` y la decide la conciliación con la auditoría (turno auditado con
tokens → se confirma; sin rastro y pasado el plazo → se libera). Es el
comportamiento que A6 pedía; lo que sigue sin existir es un resultado durable
que el cliente pueda RECUPERAR tras reconectar.

## Bloque D — servicio comercial: activar, renovar, cambiar de plan, cancelar (cerrado)

`subscription_billing_service.py` es la ÚNICA puerta por la que una
suscripción recibe un período pago. Hoy la usa `scripts/subscriptions.py`
(transferencia); Mercado Pago entrará por acá, para que un pago manual y uno
automático otorguen los mismos derechos. Reglas decididas con el dueño:

- **Activar ≠ renovar.** `activate` es para quien NO tiene período pago
  vigente ni en gracia (el ciclo arranca hoy y fija el día ancla); `renew`,
  para quien sí. Quien llama dice qué cree estar haciendo y el servicio lo
  verifica: el comando equivocado se rechaza diciendo cuál corresponde
  (`USE_RENEW` / `USE_ACTIVATE`). El `activate` viejo pisaba el período en
  curso con `ahora → ahora + 30 días`.
- **Pago anticipado** — el caso NORMAL con transferencia — otorga el período
  SIGUIENTE (`subscriptions.next_*`) sin tocar el actual ni su saldo de cupo
  (el contador es por `period_start`). Un solo período por adelantado.
  `effective_access` lo hace regir **por fecha** cuando termina el actual;
  `roll_forward` (que corre antes de cada operación comercial y en
  `expire-due`) sólo lo deja escrito — prolijidad, no requisito.
- **Pago durante la gracia:** el período nuevo corre desde el vencimiento
  ANTERIOR. Pasada la gracia es una reactivación: arranca al pagar, con ancla
  nueva.
- **Meses calendario con día ancla** (`add_months_anchored`,
  `billing_anchor_day`): 31 ene → 28 feb → 31 mar. El ancla no se pierde en
  meses cortos — igual que un débito recurrente. **Un mes por pago:**
  `--months > 1` se rechaza (sería UN cupo estirado, no varios ciclos).
- **Cambio de plan** (`change-plan`): rige desde la próxima renovación, sin
  prorrateo y sin resetear el cupo en curso. Un downgrade que deje más
  usuarios activos que plazas se rechaza diciendo cuántos sobran; no se
  desactiva a nadie. Con el período siguiente ya pago, no se puede cambiar.
- **Cancelar** (`cancel`): marca `cancel_at_period_end` — conserva todo lo
  pago y ahí corta, **sin gracia** (`cancelada_periodo_vencido`). Un `renew`
  antes de esa fecha la deshace. `expire-due` la pasa a `CANCELLED`.
- **Idempotencia:** tabla `subscription_payments` con UNIQUE
  `(source, reference)` + `INSERT ... ON CONFLICT DO NOTHING RETURNING`, bajo
  `FOR UPDATE` de la suscripción. Buscar la referencia en la auditoría antes
  de escribir (lo de antes) no resistía dos ejecuciones simultáneas: probado
  contra Postgres real. La misma referencia con OTROS datos es
  `REFERENCE_MISMATCH`, nunca un "ya aplicada" callado. La auditoría se
  conserva además del registro de pago.
- **`set-status` es reparación, no vía comercial:** no reinicia una prueba ni
  acepta `ACTIVE` sin período. `show` informa el estado EFECTIVO (el
  persistido puede estar atrasado), el período siguiente, la cancelación y
  los últimos pagos. El dry-run de los comandos EJECUTA la operación y hace
  rollback: las fechas que muestra son las que quedarían.
- Migración `20260919_0001`, aditiva, idempotente y **sin backfill**:
  `billing_anchor_day` queda NULL en filas viejas a propósito (el ancla es
  una condición comercial que se fija al cobrar, no se inventa).

**Hallazgo de paso (era de la Etapa 2, no del D):**
`test_migraciones_idempotentes_pg.py` tenía el head escrito a mano
(`20260910_0005`). Local se saltea —pide Postgres— y en CI corre, así que las
migraciones `20260917`/`20260918` ya commiteadas lo iban a poner rojo al
pushear. Ahora deriva el head de Alembic, y de paso cada migración nueva
entra sola al alcance de esa compuerta. **Lección:** antes de pushear, correr
`pytest -m postgres -n 0` contra `vektor_pgtest`; la suite SQLite no lo ve.

## Revisión cruzada de los bloques A–C

Corregido: la gracia vencida tenía dos nombres según hubiera corrido
`expire-due` (`periodo_vencido` vs `gracia_vencida`) — ahora uno solo,
`gracia_vencida`; el 409 del chat se armaba a mano fuera del traductor único
(`subscription_http_error`, que ahora lleva el `state`); `TERMINAL_STATUSES`
estaba definido y sin uso mientras el repositorio repetía el literal; textos
del script y docstrings que describían el estado anterior. Quedan anotados
para sus bloques: `cancel_at_period_end` existe pero nadie lo setea ni lo lee
(D), y `/auth/me` expone el `status` PERSISTIDO, no el efectivo — un `ACTIVE`
vencido se vería "activo" en pantalla (contrato `GET /subscription`, E).

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
- ~~Cobertura del gate transversal~~ y ~~`enforce_seat_limit` sin
  enganchar~~: cerrados en el Bloque B (ver arriba). Sigue sin gate la
  ingesta (upload/confirm/relectura): va con su cupo, en el bloque C.
- ~~Cupos de importación y foto/PDF~~ y ~~reconciliación de reservas~~:
  cerrados en el Bloque C (ver arriba).
- **Frontend**: nada de UI todavía (pantalla de facturación, aviso de
  prueba/gracia/solo lectura).
- Límites de página/MB por archivo, prorrateo de upgrades, descuentos: siguen
  fuera, como ya establecía la política.
