# B6 — Terminal habilitada y sesión que no se cae por la red

## Contexto

Dos problemas del plan del POS (`docs/plans/vektor-pos-scanner-y-ticket.md`, puntos 6 y 7):

1. **Un corte de red desloguea.** `frontend/src/lib/api.ts:136-138` hace `logout()` ante *cualquier* fallo del refresh del token, incluido uno de conectividad: el access token vence durante un corte, el refresh no llega al servidor y el usuario termina en el login. Le pasa a toda la app, no sólo a la caja.
2. **No hay forma de saber desde qué PC se cobra ni de dar de baja una.** Los refresh tokens son JWT sin estado (`app/utils/security.py`): no hay tabla que permita revocarlos. Para la caja offline (B7) hace falta una identidad de la PC, habilitada por el dueño y revocable, contra la que se validen los pendientes al sincronizar.

Decisión del usuario: **B6 completo** (interceptor + terminales), antes de B13/B12.

## Diseño

### 1. El interceptor distingue red de sesión revocada (`lib/api.ts`)
- En el `catch` del refresh:
  - **Error de red** (axios sin `response`: timeout, DNS, offline) → **no desloguea**: conserva los tokens y rechaza el request original con el error de red. El próximo request con red vuelve a intentar el refresh.
  - **Respuesta del servidor** 401/403 → sesión revocada: desloguea, como hoy.
  - **5xx** → no desloguea: el servidor está caído, no te echó.
- El `_refreshPromise` single-flight se mantiene.
- `useSessionSync` (B5) ya sigue con el rol guardado si `/auth/me` falla por red. Queda coherente con esto.

### 2. Terminales (mig `20260926_0001`)
- Tabla **`pos_terminals`**: `id`, `tenant_id`, `name`, `credential_hash` (sha256 del secreto; el secreto **nunca** se guarda), `created_by_user_id`, `created_at`, `last_seen_at`, `disabled_at`, `disabled_by_user_id`. Único `(tenant_id, name)` entre las activas.
- **`pos_operations.terminal_id`** nullable, FK `SET NULL`. Nullable porque lo histórico y las ventas cargadas por el dueño desde el navegador no tienen terminal. Rellenarlo sería inventar.
- Aditiva e idempotente (E8c + lock de `env.py`).
- `domain/pos_terminal.py` (puro): generar el secreto (`secrets.token_urlsafe(32)`) y compararlo con `hmac.compare_digest` sobre el sha256.

### 3. Qué es y qué no es la credencial de terminal
- Identifica **la PC**, no a la persona. **No reemplaza el login:** toda llamada sigue pidiendo el JWT del usuario. Así, una credencial robada no da acceso a nada por sí sola.
- Viaja en el header **`X-POS-Terminal`**. Se valida contra el tenant del usuario logueado. Una terminal de otro negocio responde como desconocida.
- Uso en B6: `POST /pos/operations` y `POST /pos/operations/{id}/void`:
  - Header válido → se guarda `terminal_id` y se actualiza `last_seen_at`.
  - Terminal dada de baja → **403 `TERMINAL_DISABLED`**. Es el código que B7 va a leer para pasar los pendientes a `REQUIERE_AUTENTICACION` y no sincronizarlos.
  - Header con un secreto desconocido → **403 `TERMINAL_UNKNOWN`**.
- **Un CASHIER sólo escribe desde una terminal habilitada:** cobrar, anular y vincular un código de barras (`POST /products/{id}/barcode`). Sin header, 403 `TERMINAL_REQUIRED`. Así un empleado no cobra desde su celular en su casa. El dueño y el admin pueden operar sin terminal, desde el navegador, como hasta hoy.
- **Crear ≠ recuperar, como en B5.** La terminal se valida sólo para una operación NUEVA, entre el lookup de la clave y el claim. Si la venta ya existe, el reintento devuelve el ticket aunque la PC haya sido dada de baja después: la venta se hizo.
- `X-POS-Terminal` **no** entra en la huella de idempotencia: el mismo pedido reintentado desde la misma PC tiene que seguir siendo el mismo.
- El secreto vive en `localStorage`, con la misma exposición que el JWT que ya está ahí. Por sí solo no da acceso a nada: siempre hace falta el login del usuario.

### 4. Endpoints (en `api/v1/pos.py`, sólo OWNER con PIN, `require_owner_stepup`)
- `POST /pos/terminals {name}` → **201 con el secreto en claro, una única vez**. Si se pierde, se da de baja y se habilita de nuevo.
- `GET /pos/terminals` → lista sin secreto, con `last_seen_at` y estado.
- `POST /pos/terminals/{id}/disable` → idempotente.
- Las tres rutas quedan fuera de la lista del cajero: el guard de B5 ya las deniega.
- Cada alta y cada baja se audita (`POS_TERMINAL_ENROLLED`, `POS_TERMINAL_DISABLED`).

### 5. Frontend
- `stores/posTerminalStore.ts`: `{ terminalId, name, secret, tenantId }` persistido con su **propia** clave de `localStorage`. **`logout()` no lo borra**: la PC sigue siendo caja aunque cambie quién está logueado.
- Interceptor de request: agrega `X-POS-Terminal` a las llamadas `/pos/*` sólo si el `tenantId` guardado coincide con el del usuario logueado.
- Ajustes → Equipo → sección **"Cajas habilitadas"** (sólo el dueño):
  - "Habilitar esta PC como caja" (nombre) → guarda el secreto en el store local.
  - La lista marca "esta PC", muestra el último uso y permite "Dar de baja".
- Si esta PC fue dada de baja (el servidor responde `TERMINAL_DISABLED`), el store se marca inválido y el panel lo muestra.

### 6. Alcance declarado respecto del plan maestro
- El test del plan "terminal deshabilitada → sus pendientes quedan `REQUIERE_AUTENTICACION`" necesita la cola de la caja, que es IndexedDB y llega en **B7**. B6 entrega el contrato del servidor (`TERMINAL_DISABLED`) y su test. El estado de los pendientes lo cablea B7 sobre ese código.
- "Operar offline con el token del usuario vencido" también es B7. En B6 lo que se garantiza es que el corte no te saque de la sesión.

## Archivos
- Nuevos:
  - `backend/app/domain/pos_terminal.py`
  - `models/pos_terminal.py`
  - migración `20260926_0001_pos_terminals.py`
  - `tests/api/v1/test_pos_terminals.py`
  - `tests/domain/test_pos_terminal.py`
  - frontend: `stores/posTerminalStore.ts`, `features/settings/components/TerminalsSection.tsx`, tests de jest del interceptor y del store
  - La migración nueva entra sola en las compuertas de migraciones idempotentes y concurrentes que ya existen.
- Backend a tocar:
  - `models/pos_operation.py` y `models/__init__.py`
  - `schemas/pos.py`
  - `api/v1/pos.py`: terminales, validación del header y `terminal_id`
- Frontend a tocar:
  - `lib/api.ts`: el catch del refresh y el header
  - `services/pos.service.ts`, que es nuevo
  - `SecurityPanel.tsx`: monta la sección
- `CLAUDE.md` en el mismo commit.

## Verificación
- **Interceptor (jest):**
  - Refresh con error de red → no desloguea, tokens intactos.
  - Refresh con 401 del servidor → desloguea.
  - Refresh con 5xx → no desloguea.
  - Dos 401 simultáneos → un solo refresh (single-flight).
- **Terminales (pytest):**
  - Alta devuelve el secreto una vez, y el listado no lo trae nunca.
  - Sólo un OWNER con PIN puede dar de alta: ADMIN y CASHIER → 403.
  - Dar de baja dos veces es idempotente.
  - Una venta con terminal válida guarda `terminal_id` y `last_seen_at`.
  - Terminal dada de baja → 403 `TERMINAL_DISABLED`, sin venta ni stock descontado.
  - Secreto desconocido o de otro tenant → 403 `TERMINAL_UNKNOWN`.
  - CASHIER sin header → 403 `TERMINAL_REQUIRED` al cobrar, al anular y al vincular un código; OWNER sin header → 201.
  - El reintento idempotente devuelve el mismo ticket, **también si la terminal fue dada de baja entre la venta y el reintento**.
- **Store (jest):** `logout()` no borra la terminal; el header no se manda si el tenant no coincide.
- **Mutation tests:** el `catch` de red, la comparación del secreto, el chequeo de baja y el requisito de terminal para el cajero.
- **Suite completa**, `-m postgres` (migración nueva, incluida la compuerta de migraciones idempotentes y concurrentes) y frontend (tsc, lint, jest, build).
