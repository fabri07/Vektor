# Hallazgos nuevos que salieron de las revisiones adversariales de cada PR

No son parte de los informes por etapa (`etapa-0-1.md`, `etapa-2.md`): los encontró el revisor de un PR mirando código vecino. Se priorizan igual que los de las etapas; ninguno está arreglado salvo que lo diga.

## De la revisión de I3-3 (permisos de importación) — 2026-10-24

| Id | Sev. | Dónde | Escenario | Arreglo propuesto |
|---|---|---|---|---|
| R-1 | grave | `api/v1/agent.py:71`, `:508-555` (`POST /agent/chat`, `/chat/stream`) | Un VIEWER escribe «gasté 5000 en luz»: `REGISTER_EXPENSE` está en `_AUTO_EXECUTE_ACTION_TYPES` y `_execute_local_action` lo persiste sin mirar el rol. | Auto-ejecutar sólo si `role_code in _CONFIRM_ROLES`; si no, pending action que apruebe un OWNER/ADMIN. |
| R-2 | normal | `api/v1/pos.py:1590`, `:1711` (`POST /pos/cash-sessions`, `/close`) | Un VIEWER abre o cierra el turno de otro con un arqueo inventado (el chequeo de dueño del turno aplica sólo a CASHIER; un arqueo cerrado no se reescribe). | `require_role("OWNER", "ADMIN", CASHIER_ROLE)`, como `/pos/operations`. |
| R-3 | normal | `api/v1/automations.py:54` (`from-pending-action`) | Un VIEWER convierte en regla una acción que aprobó el dueño; desde ahí sus pedidos de ese tipo se auto-ejecutan sin aprobación. | `require_role("OWNER", "ADMIN")`. |
| R-4 | normal | `api/v1/fields.py:94` (`POST /fields`) | Un VIEWER da de alta un campo propio (PATCH/toggle/undo ya piden `require_modify_access`). | `require_role("OWNER", "ADMIN")`. |
| R-5 | menor | `api/v1/agent.py:1666`, `:1729` (`/agent/cancel`) | Un VIEWER cancela la acción pendiente de otro usuario del tenant. | Exigir el autor o OWNER/ADMIN. |
| R-6 | menor | `api/v1/files.py:45` (`purpose != "chat"`) | Un VIEWER sube hasta 16 MB a S3 sin gate (queda PENDING; confirm/reprocess ya cerrados). Relacionado con I3-8. | Aceptar sólo `chat`, o pedir OWNER/ADMIN para otro `purpose`. |
| R-7 | menor | `others.py`, `customers.py`, `suppliers.py`, `products.py` | El rol está como parámetro y el gate de suscripción en el decorador: un VIEWER con la suscripción vencida recibe 402 en vez de 403. | Mover el rol al decorador, primero. |
| R-8 | menor | `deps.require_role` | El detalle del 403 está en inglés y la pantalla lo muestra tal cual. | Mensaje en español. |

## De la revisión de T1 (cobro de fiado) — 2026-10-24

| Id | Sev. | Dónde | Escenario | Arreglo propuesto |
|---|---|---|---|---|
| R-9 | menor | `transaction_repository.get_daily_net_series` → `health/agent.py:646` | El agente de salud la manda como `domain="caja"`, pero es ventas − gastos (incluye fiado, no los cobros). Anterior a T1. | Rotularla «resultado» o calcularla con la partición de caja. |
| R-10 | normal | `forecast_service.ingresos_de_caja_por_dia` | La tarjeta de crédito se cuenta el día de la venta (FactsService la saca de la caja de hoy). Decisión del dueño: PR aparte, reflejar la acreditación esperada, no 30 días fijos; si se estima, explícito y configurable. | PR aparte. |
| R-11 | normal | `cash_close_service` (sobrante) + `frontend/src/lib/payment.ts` | El sobrante del arqueo se guarda como `inflow` y se rotula «Cobro cta. cte.». Decisión del dueño: separar su clasificación en otro PR. | Método propio para el sobrante. |
