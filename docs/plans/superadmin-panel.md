# Panel Superadmin — contrato (F0)

Fase 0 del panel de plataforma (modelo: el panel de SUPERADMIN de app-gim). Este documento fija el contrato ANTES de tocar código: qué significa cada estado de un tenant, quién lo hace cumplir y qué pasa con la caja offline. Las fases F1–F7 lo implementan, un PR cada una.

Decisiones del dueño: alcance de paridad completa con app-gim; suspensión en dos niveles (`READ_ONLY` y `SUSPENDED`); sin impersonación; las ventas offline ya cobradas anteriores al corte se sincronizan.

## 1. Dos controles distintos

| Control | Dónde vive | Quién lo gobierna | Lo cambia |
|---|---|---|---|
| **Estado de cuenta** | `tenants.status` (Text, sin CHECK) | política de acceso (F1) | SUPERADMIN, por `/admin/tenants/{id}/status` |
| **Estado de suscripción** | `subscriptions.status` (CHECK: TRIAL, ACTIVE, GRACE, READ_ONLY, CANCELLED) | `subscription_service` | ciclo comercial (activar, renovar, vencer, cancelar) |

Son independientes: un tenant puede tener la cuenta `ACTIVE` y la suscripción `READ_ONLY` por falta de pago, o la cuenta `SUSPENDED` con la suscripción al día (decisión del soporte). El panel los muestra **por separado** y los mensajes de rechazo no se mezclan: códigos propios para cada uno (`TENANT_*` vs `SUBSCRIPTION_*`).

## 2. Matriz de comportamiento del estado de cuenta

Estado desconocido (cualquier valor fuera de la tabla, incluido `None`) = **falla cerrada**: se trata como `SUSPENDED`.

| Estado | Login / refresh / OAuth | Lecturas | Escrituras | Caja offline | SUPERADMIN |
|---|---|---|---|---|---|
| `ACTIVE` | sí | sí | sí | sí | n/a |
| `TRIAL` | sí | sí | sí | sí | n/a |
| `READ_ONLY` | **sí** (hoy no) | sí | no → `TENANT_READ_ONLY` | ver §5 | no afectado |
| `SUSPENDED` | no → `TENANT_SUSPENDED` | no | no | ver §5 | no afectado |

`READ_ONLY` de cuenta no reemplaza al `READ_ONLY` de suscripción: el primero lo decide el soporte, el segundo el ciclo comercial. Si ambos aplican, gana el más restrictivo, y el código devuelto es el del control que bloquea primero (cuenta antes que suscripción).

## 3. Estado actual (verificado en el código, 2026-10-29)

- `get_current_tenant` (`api/v1/deps.py:126`) rechaza con 403 todo estado ≠ `ACTIVE`/`TRIAL`, pero solo las rutas que lo inyectan. `get_current_user` no mira el estado: una ruta que depende solo del usuario sigue funcionando con el tenant suspendido.
- Login (`auth_service.py:158`) devuelve `None` (401 genérico) para todo estado ≠ `ACTIVE`/`TRIAL`. Por eso `READ_ONLY` no puede entrar hoy.
- Refresh (`auth_service.py`, ~273-292) solo chequea tenant inexistente y demo bloqueado: **un tenant suspendido sigue renovando tokens**.
- Google OAuth acuña tokens en `google_oauth_service.py` (~666) sin una comprobación visible del estado del tenant. Verify-email también. F1 debe revisar y cubrir con test cada una de las emisiones.
- Los jobs filtran el estado con valores distintos: `send_weekly_email` (`== "ACTIVE"`), `score_worker` (`in ["active","trial"]`, minúsculas), `inventory_integrity_check` (`["ACTIVE","TRIAL"]`), `update_momentum` (`["active","trial","ACTIVE"]`). **Incoherente**: el de `score_worker` no matchea con las mayúsculas que guarda el resto. Se unifica en F1 con una constante compartida; es un hallazgo de F0, no se arregla acá.
- Nada del código de la app asigna `SUSPENDED`.

## 4. Qué cuenta como escritura

No se asume que todo `POST` escribe ni que todo `GET` es inocuo. F1 parte de un inventario hecho sobre las rutas reales:

- Se **reutiliza la clasificación por efecto** del gate de suscripción (`docs/claude/suscripciones.md`), que ya distingue altas y consumo de IA de `PATCH`/`DELETE` y de la caja. Hay que decidir ruta por ruta cuáles de esas excepciones aplican también al estado de cuenta: para `READ_ONLY` de cuenta, **la caja no se exceptúa por defecto** (es lo que el soporte quiere frenar), pero la venta offline ya cobrada sí se conserva (§5).
- El test estructural `test_las_rutas_con_gate_son_exactamente_estas` sigue verde. Las rutas `/admin/*` **no** llevan el gate de suscripción del tenant propio.
- Los `POST` de solo lectura (extracciones, previews, `export`) se listan explícitamente como permitidos en `READ_ONLY`; el default es bloquear lo no clasificado.

## 5. Caja offline

Hecho del código: una venta cobrada sin conexión llega con `X-POS-Offline`, `charged_at` (obligatorio, `OFFLINE_WITHOUT_CHARGED_AT` si falta), y desde una terminal habilitada (`TERMINAL_REQUIRED_FOR_CAPTURE`). Nada de eso **prueba** que el efectivo entró: deja la afirmación atribuida a un usuario, una PC y un momento. Un reintento de una operación ya creada devuelve el ticket aunque cambien los permisos.

Política decidida:

1. **Venta con `charged_at` anterior al corte** (momento en que la cuenta pasó a `READ_ONLY`/`SUSPENDED`): se acepta al reconectar. El dinero ya se cobró y tiene que quedar registrado. Reutiliza la idempotencia por `client_operation_id`.
2. **Venta con `charged_at` posterior al corte**: se rechaza con `TENANT_SUSPENDED`/`TENANT_READ_ONLY`, pero **nunca se borra de la cola** del navegador: queda conservada y visible con el motivo, para reenviarse al reactivar. Ninguna respuesta del servidor borra una venta pendiente (regla de `pos-offline.md`).
3. **Reintento de una operación que ya existe**: se devuelve el ticket sin evaluar el estado, como hoy con los permisos.

Riesgo asumido y a acotar en F1: `charged_at` lo declara el cliente, así que un cliente podría fechar hacia atrás para colar una venta tras el corte. Mitigaciones: (a) solo se acepta desde una terminal habilitada (ya exigido); (b) se limita la antigüedad máxima de una venta offline (valor a fijar, p. ej. 72 h) y que no sea futura (hoy `fecha_no_futura` en `schemas/pos.py` solo rechaza un DÍA de negocio posterior a hoy: no acota el instante ni la antigüedad, así que ambas cotas son nuevas); (c) cada venta aceptada tras un corte queda marcada en la auditoría con `accepted_after_status_change`. Para comparar contra el corte hace falta saber **cuándo** cambió el estado: se agrega `tenants.status_changed_at` (aditiva, nullable) en F2, junto con el endpoint que cambia el estado. Mientras no exista la columna, F1 solo puede aplicar el criterio de §2 sin la excepción de §5.1 (por eso la excepción se implementa en F2, no en F1).

## 6. Sesiones y tokens ya emitidos

- Las sesiones se revalidan en cada request: `get_current_user` lee usuario y estado de base (el rol del JWT no se confía; hoy ya ocurre con CASHIER) → una suspensión corta la sesión en el siguiente request, sin esperar a que expire el token.
- Refresh, verify-email, Google OAuth y login aplican la **misma política central**, no copias.
- **SUPERADMIN inmune**: vive en un tenant propio (`bootstrap_superadmin.py`) y no debe quedar afuera si ese tenant cambia de estado. La excepción se evalúa por rol leído de base, no por el claim del JWT.
- Una sola consulta por request: la política cachea el tenant en `request.state`.

## 7. Pagos

`SubscriptionPayment` (`persistence/models/tenant.py:229`, tabla `subscription_payments`) ya es el registro comercial: insert-only, UNIQUE `(source, reference)` como candado de idempotencia, período, importes USD/ARS, operador, copia de lo otorgado. Es el mismo hecho que el "pago manual" del panel. **No se crea una tabla paralela.** F4 la extiende (source `manual`/`transfer`) y define pagos parciales, ajustes y correcciones sin imponer un pago único por período.

## 8. Rutas admin actuales (punto de partida)

Todas con `require_role("SUPERADMIN")`, `tenant_id` explícito en el path cuando hace falta:
`GET /admin/metrics`, `/admin/analytics/benchmarks`, `/admin/usage`, `/admin/repairs/*`, `/admin/inventory-integrity/{tenant_id}`, `/admin/pipeline/*`, `/admin/ingestion/reread-health`, y la cola `/admin/access-requests` (listar, aprobar, rechazar, posponer). No existen: listado/ficha de tenants, cambio de estado, cambio de plan desde API (hoy solo `scripts/subscriptions.py`).

## 9. Fases

| Fase | Entrega | Depende de |
|---|---|---|
| F0 | este documento | — |
| F1 | política central de estado, aplicada en `get_current_user`, login, refresh, verify-email y OAuth; códigos estables; pantalla de cuenta bloqueada; constante única de estados para los jobs | F0 |
| F2 | `/admin/tenants` (lista, ficha, cambio de estado auditado e idempotente); `tenants.status_changed_at`; excepción de caja offline (§5.1) | F1 |
| F3 | acciones de suscripción por API sobre los servicios existentes | F2 |
| F4 | pagos manuales sobre `subscription_payments`; add-ons (requiere catálogo) | F3 |
| F5 | telemetría de actividad diaria | F2 |
| F6 | panel de frontend | F2–F5 |
| F7 | hardening, PIN/2FA, despliegue y reversión | F6 |

Pendientes del dueño: catálogo de add-ons y moneda de los pagos manuales (antes de F4); PIN/2FA para acciones destructivas (antes de F7); antigüedad máxima de una venta offline (F1).

## 10. Fuera de alcance

Impersonación; moderación de tienda (Véktor no tiene tienda); cobro automático.
