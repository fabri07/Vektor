# B5 — Rol `CASHIER`, catálogo POS sin costos y permisos por cajero

## Contexto

La caja la va a operar un empleado, no el dueño. Hoy existen cuatro roles (`OWNER`, `ADMIN`, `ANALYST`, `VIEWER`), y **200 de las 218 rutas autenticadas** sólo piden un usuario logueado. Si un cajero fuera un usuario más, podría leer por la API todo lo que el dueño ve: costos (`GET /products` devuelve `unit_cost_ars` y `margin_pct`), márgenes, caja, dashboard y el chat con el agente.

B5 agrega el rol `CASHIER` con **acceso denegado por defecto**: un cajero sólo alcanza una lista cerrada de rutas, y dentro de ellas, sólo lo que su dueño le habilitó.

Decisiones tomadas:
- **Permisos configurables por cajero**, con migración y panel en `/settings`.
- **El cajero ocupa plaza del plan.**

Una revisión externa del plan encontró ocho puntos más, todos verificados contra el código. Se incorporan acá (marcados **[R1]…[R8]**). Tres son bugs de lo que ya está desplegado:
- **[R3]** Una fecha con zona se guarda sin convertir.
- **[R5]** Pasar a un usuario a cajero conserva `can_modify_sensitive`.
- **[R7]** El login busca por email entre todos los negocios.

## Diseño

### 1. Denegar por defecto, en un solo punto
- En `api/v1/deps.py::get_current_user`, si `role_code == "CASHIER"` y la ruta (`request.scope["route"].path` + método) no está en `CASHIER_ALLOWED_ROUTES`, responde **403 `CASHIER_ROUTE_FORBIDDEN`**.
  - Mira el rol **de la base**, no el del JWT, así que un cambio rige desde el request siguiente.
  - `get_current_user` es el único lugar que decodifica tokens (verificado): no hay otra puerta sin guard.
  - Una ruta nueva nace cerrada para el cajero.
- Lista cerrada de rutas permitidas, en `domain/pos_permissions.py`:
  - Sesión: `GET /auth/me`, `POST /auth/refresh`, `/auth/logout`, `/auth/change-password`, `GET /auth/pin/status`, `POST /auth/pin/{setup,verify,change,reset}`, `GET` y `PATCH /users/me`.
  - Caja: `GET /pos/catalog`, `GET /products/lookup`, `POST /pos/operations`, `POST /pos/operations/{id}/void`, `POST /products/{id}/barcode`, `GET /pos/customers`.
- **El chat queda afuera.** El agente lee todo el negocio.

### 2. Permisos por cajero (mig `20260925_0001`)
- `users.pos_permissions JSONB NOT NULL DEFAULT '{}'`. Aditiva e idempotente (E8c, protegida por el lock de `env.py`).
- `domain/pos_permissions.py` define el set cerrado y puro de `PosPermission`: `discount`, `void_ticket`, `fiado`, `learn_barcode`.
- `effective_pos_permissions(user)`:
  - OWNER y ADMIN tienen todos, y nada de lo actual cambia.
  - CASHIER tiene sólo los permisos cuyo valor es **exactamente `true`** (booleano). `"true"`, `1`, `null`, lo ausente y las claves desconocidas no habilitan nada.
  - Los demás roles no tienen ninguno.
- La dependency `require_pos_permission(p)` responde 403 `POS_PERMISSION_DENIED` con el permiso.
- "Cambio de precio" y "resolver discrepancias" entran cuando exista la acción que protegen (B7/B11).

### 3. Reglas en las rutas de caja
- **`POST /pos/operations`**: `require_role(OWNER, ADMIN, CASHIER)`. **[R6] Crear y recuperar se autorizan distinto.**
  - Autenticación y tenant se verifican siempre.
  - Si la clave de idempotencia ya existe (`Repeticion`), se devuelve la operación **sin** reevaluar `discount`/`fiado`. Una venta que ya se hizo se puede recuperar aunque después le revoquen el permiso.
  - Sólo una operación NUEVA exige los permisos actuales.
  - El chequeo va entre el lookup de la clave y el claim, así que un 403 no la consume.
  - Una venta offline no registrada y rechazada por permiso queda `FAILED` en la cola, sin descartarse ni atribuirse a otro cajero. La derivación a revisión es de B7; acá queda anotada.
- **`POST /pos/operations/{id}/void`**:
  - OWNER/ADMIN siguen con `require_modify_access`, como hoy.
  - **[R8]** CASHIER necesita `void_ticket`, la ventana de **su** PIN, que el ticket sea **suyo** (`created_by_user_id == user.id`; uno ajeno da 403 `VOID_NOT_OWN_TICKET` y lo resuelve el encargado) y **`reason` obligatorio**.
  - `POST /auth/pin/setup` (`auth.py:278`) y `PinService` (`pin_service.py:112`) se amplían a "o CASHIER con `void_ticket`". Sin eso el permiso es inalcanzable.
- **`POST /products/{id}/barcode`**: CASHIER con `learn_barcode`. **[R1]** Pasa a responder `PosProductResponse`, también en el reintento idempotente.
- **`GET /products/lookup`**: se mantiene el contrato `{code_type, matched_by, product}` de B4 y sólo `product` pasa a `PosProductResponse`.

### 4. Catálogo POS [R2]
- `PosProductResponse`:
  - `id`, `name`, `internal_sku`, `sku`, `barcode`, `sale_price_ars`, `sale_unit`, `base_units_per_sale_unit`.
  - **`stock_units`** (unidades base, para descontar localmente en B7). `stock_display` es sólo presentación y nunca se parsea.
  - `stock_status`.
  - **Sin** `unit_cost_ars`, `margin_pct`, `list_price_ars` ni `custom_fields`.
- `GET /pos/catalog?q=&limit=&after=`: productos activos con precio mayor a 0, **orden estable por `id` y cursor `after`**, así B7 descarga el catálogo completo de forma determinística. Versión de sincronización y bajas quedan declaradas para B7.
- `GET /pos/customers?q=`: sólo `id` y `name`, para elegir a quién se le fía.

### 5. Precio por kilo/litro: se cierra el contrato [R4]
- **Contrato:** `sale_price_ars` y `unit_cost_ars` son **por unidad de venta** (por kg, por L, por unidad); la cantidad viaja en unidades base.
- `domain/pos_operation.py`: bruto de línea = `precio × cantidad / factor`, redondeado **una vez, al centavo, por línea** (half-up). $1.234,56/kg × 750 g = $925,92. Con factor 1 el resultado es idéntico al actual.
- `LineaPedida` gana `base_units_per_sale_unit`.
- Valorización de stock: helper puro `valor_de_stock(stock_units, factor, costo)` en `domain/sale_unit.py`, usado por `economic_summary_service.py:40` y por la valorización de `FactsService`. Los lectores `stock_units × unit_cost_ars` se enumeran con grep y se cubren todos.
- Sigue **sin** existir camino de escritura para `sale_unit`: el schema de alta no lo acepta. Ningún producto puede estar hoy en gramos. Se habilita con la pantalla de caja, ya con el contrato cerrado.

### 6. Fechas: se normaliza lo que se guarda [R3]
- `fecha_de_negocio` pasa a acompañarse de `a_hora_de_negocio(dt) -> datetime naive AR`: un datetime con zona se convierte a hora argentina y se le saca la zona; uno sin zona queda igual.
- Los validadores anti-futuro de `schemas/pos.py`, `schemas/transaction.py` y `schemas/purchase.py`, y `others.py`, **devuelven el valor normalizado**, no el original.
- Ejemplo: `2026-09-24T02:00:00Z` se guarda como `2026-09-23 23:00:00` y la venta cae en el día 23.

### 7. Alta, cambio de rol y email [R5][R7]
- `schemas/user.py`: `CASHIER` en los dos regex.
- **Transición de rol** (`api/v1/users.py::update_user` y alta):
  - **→ CASHIER**: `can_modify_sensitive = false`, `pos_permissions = {}` (arranca sin nada) y se invalida la ventana de PIN (`pin:verified:*`).
  - **CASHIER → otro rol**: `pos_permissions = {}`, para que no reaparezcan si vuelve a ser cajero.
  - Cada cambio de rol o de permisos va a `decision_audit_log` con antes y después.
- `PATCH /settings/team/{id}`:
  - Acepta `pos_permissions` sólo sobre un CASHIER; si no, 400.
  - Rechaza `can_modify_sensitive=true` sobre un CASHIER (400).
  - Audita el cambio.
- **[R7] Email:** el login es global (`get_by_email_any_tenant`, `limit(1)`), así que el alta de un usuario **rechaza un email que ya existe en otro tenant**, con 409 `EMAIL_IN_USE_OTHER_BUSINESS`. Selección de negocio en el login queda fuera de B5.
- Plazas: el CASHIER cuenta, sin cambios en `enforce_seat_limit`.

### 8. Frontend
- `SecurityPanel.tsx` → `TeamSection` (sólo OWNER):
  - Formulario **"Agregar cajero"** (nombre, email, contraseña inicial) → `POST /users` con `role_code: "CASHIER"`, mostrando el 409 de plazas y el de email.
  - Los cajeros se listan con el rótulo "Cajero" y cuatro casillas.
- Rótulos legibles del rol en `Sidebar.tsx:279` y en `settings/page.tsx:407`.
- **[R8] Guard del layout protegido:**
  - Al montar y al volver el foco, **refresca `/auth/me`**, no confía en el rol guardado en `authStore`.
  - Si el usuario pasó a CASHIER o cambió, limpia la caché de TanStack Query antes de mostrar nada.
  - Un CASHIER ve "Tu usuario es de caja. La pantalla de caja todavía no está disponible" hasta B13, sin la navegación del dueño.

## Archivos
- Nuevos:
  - `backend/app/domain/pos_permissions.py`
  - `migrations/versions/20260925_0001_user_pos_permissions.py`
  - `backend/app/tests/api/v1/test_cashier_access.py`
  - `backend/app/tests/domain/test_pos_permissions.py`
- Backend:
  - `models/user.py`
  - `schemas/user.py`, `schemas/pos.py`, `schemas/transaction.py`, `schemas/purchase.py`, `schemas/auth.py`
  - `api/v1/deps.py`, `api/v1/pos.py`, `api/v1/products.py`, `api/v1/users.py`, `api/v1/settings.py`, `api/v1/auth.py`, `api/v1/others.py`
  - `application/services/pin_service.py`, `domain/pos_operation.py`, `domain/sale_unit.py`, `domain/business_time.py`
  - `economic_summary_service.py`, los lectores de valorización de `FactsService`
- Frontend: `SecurityPanel.tsx`, `security.service.ts`, `users.service.ts`, el layout protegido, `Sidebar.tsx`, `settings/page.tsx`, `types/api.ts`.
- `CLAUDE.md` en el mismo commit.

## Verificación
- **Compuerta estructural** `test_rutas_del_cajero_son_exactamente_estas`: la lista de rutas permitidas coincide exactamente con rutas reales, y cualquier otra ruta autenticada da 403 para un CASHIER.
- **Recorrido real con un CASHIER**: `GET /products`, `/sales`, `/economic-summary`, `/insights/current` y `/agent/chat` dan 403.
- **Sin campos sensibles en las tres respuestas** (`/pos/catalog`, `/products/lookup`, `POST .../barcode` incluido el reintento): ni `unit_cost_ars`, ni `margin_pct`, ni `list_price_ars`, ni `custom_fields`.
- **Matriz de permisos:**
  - Cada permiso probado con y sin él.
  - Valores malformados (`"true"`, `1`, `null`) y claves desconocidas → no habilitan.
  - OWNER y ADMIN sin cambios.
- **[R6]** Respuesta perdida seguida de revocación: el reintento devuelve el ticket (200), no un 403. Un rechazo por permiso no consume la clave.
- **[R8]** Un cajero no anula el ticket de un compañero; sin `reason`, 422.
- **[R5]** Conversión ADMIN→CASHIER limpia `can_modify_sensitive` y la ventana de PIN. CASHIER→ADMIN→CASHIER no recupera los permisos viejos. Todo queda auditado.
- **[R7]** Alta con un email que existe en otro tenant → 409.
- **[R3]** Venta con `...Z`: se relee la fecha guardada y el día imputado. Corre también en `-m postgres`.
- **[R4]** Venta fraccionada completa: 750 g de un producto en gramos a $1.234,56/kg da $925,92, más costo, margen y valorización de stock. Productos `unit/1` sin cambios, con el barrido existente.
- **Catálogo:** paginación por cursor que recorre todo sin repetir ni saltear.
- **Mutation tests:** el guard de `get_current_user`, el chequeo `is True`, la exclusión de costos, la división por factor y la normalización de fecha.
- **Suite completa**, `-m postgres` (hay migración) y frontend (tsc, lint, jest, build).
