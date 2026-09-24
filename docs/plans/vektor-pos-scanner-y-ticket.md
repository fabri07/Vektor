# Véktor POS — scanner de código de barras + ticket

## Contexto

Véktor hoy captura datos de tres maneras: importación de archivos, chat con agentes, y carga manual en un modal. Ninguna sirve para el momento en que el negocio realmente vende: el mostrador. El dueño carga sus ventas *después*, de memoria o de un cuaderno, y por eso los datos que Véktor analiza llegan tarde, incompletos y sin desglose por producto.

Incorporar hardware cambia eso: si Véktor es la caja, el dato nace correcto y en el momento. Cada venta escaneada trae producto, cantidad, precio real y medio de pago sin que nadie transcriba nada, y el stock se descuenta solo.

**Esto cambia qué es Véktor.** Hoy, si Véktor se cae, el dueño no ve su dashboard. Con caja, si Véktor se cae, el negocio no vende. El plan asume ese compromiso explícitamente: una operación con identidad propia, offline que sobrevive al cierre del navegador, y una caja que nunca se bloquea por administración.

Este plan incorpora una revisión externa que corrigió diez puntos del borrador anterior. **Las diez correcciones fueron verificadas contra el código** antes de aceptarse.

### Decisiones tomadas

| Decisión | Elección |
|---|---|
| Alcance | Modo caja completo, los tres rubros |
| Dispositivo | PC/notebook Windows + Chrome |
| Scanner | USB HID keyboard-wedge, **2D imager** |
| Impresión | `window.print()` en iframe + Chrome `--kiosk-printing`. **Cero `.exe`** |
| Ticket | Comprobante **no fiscal**. ARCA/AFIP fuera de scope |
| Offline | PWA + **toda la operación en IndexedDB antes del primer envío** |
| Suscripción vencida | La caja sigue vendiendo; se bloquea el análisis |
| Stock offline insuficiente | Separar la plata del stock: la venta entra, quedan **salidas pendientes** |
| Cajero | Rol `CASHIER` + catálogo propio sin costos + permisos granulares |
| Cantidades fraccionadas | **Unidades base enteras** (gramos, mililitros) |
| Devoluciones | **Solo anulación completa del ticket** en v1 |
| Arqueo | **Sesiones de caja** (apertura/cierre por cajero) |

---

## El hardware (guía para el usuario final)

**El código de Véktor no cambia según la marca del aparato.** Un lector USB se presenta al sistema operativo como un teclado: escanea y "tipea" los dígitos seguidos de un Enter. Honeywell, Zebra, Datalogic o un genérico emiten la misma secuencia. Lo único que varía por marca es la *configuración del aparato* (sufijo, simbologías), que se hace escaneando códigos del manual del fabricante — una vez, por el usuario, sin software.

- **Lector: 2D imager USB en modo HID.** Un 1D láser **no lee QR**. El 2D cubre EAN-13/UPC, Code128 y QR.
- **Impresora: térmica 80mm con driver de Windows.** Para cajón de dinero se activa "abrir cajón al imprimir" en las propiedades del driver.
- **Chrome con `--kiosk-printing`** en el acceso directo.

**Descartado: sistemas embebidos IBM/Toshiba 4690** (SurePOS / TCx): hardware propietario, ecosistema cerrado, modelo opuesto al de un SaaS multi-tenant para PYMEs.

### Cómo cada rubro llega a tener códigos

Hoy **ningún producto tiene `barcode` cargado** y el frontend ni siquiera expone el campo.

- **Kiosco/almacén** — el producto ya trae EAN de fábrica. → **Modo aprendizaje**: se escanea un código desconocido, Véktor pregunta a qué producto corresponde y lo asocia.
- **Decoración y limpieza** — mucho producto sin código de fábrica. → **Véktor imprime la etiqueta** con el `internal_sku` que ya genera y garantiza único por tenant (`VKT-…`, `product.py:53`), en Code128.

---

## Lo que ya existe y se reusa

- `POST /sales/manual-batch` (`api/v1/sales.py:277-403`): carrito de líneas, `SELECT ... FOR UPDATE` ordenado por id, validación de stock, `decrement_for_sale` por línea, atómico. **Es la base de la escritura, no el contrato del POS** (ver corrección 1).
- `products.barcode` + `barcode_normalized` con único parcial e índices (`models/product.py:67-68,119,131-142`). El backend **ya expone `barcode`** en los tres schemas (`schemas/product.py:30,114,139`).
- `product_identity_guard` + los 409 `DUPLICATE_PRODUCT_IDENTITY` / `IDENTITY_CONFLICT` (`services/product_identity.py:471-507`).
- `trigger_score_recalculation_after_commit` (`services/score_trigger_service.py`) — el helper post-commit que hoy `manual-batch` **no** usa.
- `guarded_savepoint`, `acquire_write_lock_shared`, `decision_audit_log`, `today_ar()`.

---

## Los diez hallazgos verificados

| # | Hallazgo | Evidencia |
|---|---|---|
| 1 | `sale_group_id` vive en `custom_fields` y el PATCH/DELETE actúa sobre **líneas sueltas** — "anular la venta anula sus tenders" rompería un ticket parcial | `sales.py:378,473-601` |
| 2 | El prorrateo de descuento **no cierra**: `unit_price` tiene `decimal_places=2`, y $1 sobre 3×$100 no da ningún unitario válido | `schemas/transaction.py:136` |
| 3 | La cola offline **envía primero y encola después**; si el navegador se cierra en el medio, la venta no existe en ningún lado. Y `QueuedItem` no lleva tenant ni cajero | `offlineQueueStore.ts:6`, `useOfflineSubmit.ts:191-227` |
| 4 | `claim_idempotency_key` devuelve un `bool`: no guarda la respuesta ni compara el contenido. "Guardó la venta pero no recibí la respuesta" es irrecuperable | `services/idempotency.py` |
| 5 | `ProductResponse` incluye `unit_cost_ars` y `margin_pct`: reusarlo para el catálogo offline le entrega los costos al cajero | `schemas/product.py:18-101` |
| 6 | `catch (refreshError) { logout() }` no distingue red de sesión revocada: un corte con el access token vencido manda al cajero al login | `lib/api.ts:136-138` |
| 7 | `quantity` y `stock_units` son `Integer`: no hay 0,750 kg | `models/transaction.py`, `models/product.py:84` |
| 8 | `normalize_barcode` **borra letras y símbolos**: un alfanumérico con 8 dígitos puede colisionar con el barcode de otro producto | `domain/text_norm.py:70-85` |
| 9 | `get_receivables_by_customer` filtra `payment_method == 'account'`: es un **octavo** lector, y una venta `mixed` le perdería la parte fiada | `transaction_repository.py:332-346` |
| 10 | `trigger_score_recalculation.delay(...)` corre **dentro de la transacción, antes del return**: Redis caído tira el cobro | `sales.py:398` |

Más: `manual-batch` y `create_sale` **no persisten `unit_price`**; los 7 validadores de fecha usan `date.today()` (UTC de servidor) y por eso aceptan una venta fechada mañana en AR entre las 21:00 y la medianoche; `size: 80mm auto` no es sintaxis válida de `@page` (mezclar `<length>` con `auto` no está permitido en CSS Paged Media 3); y `role_code` es `Text` sin CHECK, así que `CASHIER` no necesita migración.

---

## Las decisiones de diseño

### 1. La operación es la unidad, no la línea

`sale_group_id` dentro de `custom_fields` no es una identidad: no se puede referenciar, ni bloquear, ni auditar como un todo. Tabla **`pos_operations`** como cabecera (tenant, terminal, cajero, sesión de caja, fecha, total, `cash_received`, `cash_change`, estado, `client_operation_id`), con **`pos_operation_lines`** y **`pos_tenders`** colgando. Cada línea guarda el `sale_entry_id` que produjo — las `SaleEntry` siguen siendo la fuente de verdad contable y el histórico no se toca.

`client_operation_id` se genera **antes del primer envío** y viaja como `Idempotency-Key`: eso es lo que hace recuperable una respuesta perdida.

**El PATCH genérico se bloquea sobre ventas que pertenecen a una `pos_operation`** (409 `BELONGS_TO_POS_OPERATION`). Corregir un ticket es anular la operación, que revierte líneas, pagos y stock atómicamente y queda auditado. Devolución parcial: fuera de v1, declarado.

### 2. Descuentos sobre totales de línea, no sobre el unitario

El unitario de dos decimales no puede absorber un descuento indivisible. Cada línea guarda tres campos: `unit_price_list` (precio de lista al momento), `discount_ars` (descuento monetario de la línea) y `line_total` (lo que se cobra). El descuento global se reparte **entre `line_total`**, en centavos enteros, con el residuo a la línea de mayor importe — determinístico y sin pérdida.

La igualdad que valida el backend es **`Σ tenders = Σ line_total`**. El backend **recalcula** `line_total` desde el catálogo y el descuento; no confía en el número del cliente. El permiso de descuento es explícito por rol.

`cash_received` y `cash_change` van en la cabecera y **no son la venta**: entregar $10.000 para pagar $8.500 registra $8.500 de venta y $1.500 de vuelto.

### 3. Cantidades: unidades base enteras

`products.sale_unit` (`unit` | `gram` | `milliliter`) + `products.base_units_per_sale_unit` (Integer, default 1). `stock_units` y `quantity` pasan a contar **unidades base**; con el default de 1 para todo lo existente, ningún número cambia y no hay migración de tipo — que sobre dos columnas que atraviesan el motor de inventario, el integrity check y la reconciliación temporal sería el mayor riesgo de regresión del proyecto.

**Riesgo declarado:** un producto en gramos tendrá `stock_units = 5000` para 5 kg. Toda superficie que *muestre* stock a un humano tiene que leer `sale_unit` — `ProductResponse` gana `sale_unit` y `stock_display`. Lo que hace *aritmética* (integrity, reconciliación, FactsService) no cambia, porque todo es consistente en unidad base.

**Pack y variante fuera de v1, declarado:** un pack de seis es un producto propio con su propio código. **No hay conversión automática pack → unidad**, y el plan lo dice en vez de dejar que descuente mal.

### 4. El escaneo falla ante ambigüedad

`normalize_barcode` borra letras, así que resolver "el texto entero como identidad" puede elegir el producto equivocado. El nuevo `domain/scan_code.py` **clasifica el código antes de buscar** y consulta solo la columna de ese tipo:

| Tipo | Regla | Columna |
|---|---|---|
| `INTERNAL_SKU` | `^VKT-[A-Z0-9]{12}$` | `internal_sku` |
| `GTIN` | 8/12/13/14 dígitos **con checksum válido** | `barcode_normalized` |
| `SCALE` | prefijo de balanza (EAN-13 `2x`) | producto + peso/importe embebidos |
| `OPAQUE` | cualquier otra cosa | `sku_normalized`, **literal, sin extraer dígitos** |

Si aparecen candidatos por tipos distintos → **`409 SCAN_AMBIGUOUS` con los dos**, nunca elegir. Los códigos de balanza necesitan parser propio (producto + cantidad/importe); si el piloto no los cubre, se declaran fuera de v1 — resolver el texto completo como identidad es exactamente el error.

El aprendizaje offline se guarda en estado `PENDING`. Al sincronizar, si otra caja ya vinculó ese código a otro producto, **queda para revisión y no modifica ventas anteriores**.

### 5. Idempotencia que devuelve el resultado

`claim_idempotency_key` devuelve `bool` y responde 409 vacío. Para una caja hay que resolver *"el servidor guardó la venta pero nunca recibí la respuesta"*. Tabla **`idempotency_records`**: `(tenant_id, key)` único, `request_hash`, `response_json`, `status`, `created_at`.

- misma key + mismo `request_hash` → **200 con la operación original** (ticket, pagos y discrepancias incluidos).
- misma key + hash distinto → **409 `IDEMPOTENCY_KEY_REUSED`**, explícito.
- Reimprimir o recuperar una respuesta perdida **nunca** crea otra venta.

Se cablea primero en las rutas POS; el resto puede migrar después. Va antes de habilitar cualquier venta.

### 6. Offline: escribir antes de enviar

El flujo actual intenta enviar y encola solo si detecta error de red. Si el navegador se cierra durante el envío, la venta no queda en ningún lado.

La operación, sus pagos, el pendiente de sincronización y el **efecto local de stock** se escriben en **una sola transacción de IndexedDB, antes del primer envío**. Asociados a tenant, terminal y cajero, y esa pertenencia se valida al sincronizar — conservar la cola al cerrar sesión sin verificar identidad permite sincronizar bajo otra sesión.

Estados visibles: `PENDIENTE`, `SINCRONIZADA`, `REQUIERE_AUTENTICACION`, `REQUIERE_REVISION`. **Ninguna venta cobrada se borra jamás por cantidad de reintentos.** Coordinación entre pestañas con Web Locks. Se pide `navigator.storage.persist()` — IndexedDB solo no garantiza que el navegador no evacúe datos — y se ofrece exportación de pendientes como último recurso.

### 7. Sesión offline separada de la renovación del token

`lib/api.ts:136-138` hace `logout()` ante cualquier fallo del refresh, incluido uno de conectividad. Preservar la cola no evita que el cajero termine en el login durante un corte.

**`pos_terminals`**: una terminal se habilita una vez (acción del OWNER) y recibe una credencial de larga duración propia, independiente del refresh del usuario. El interceptor distingue **fallo de red** (reintenta, no desloguea) de **sesión revocada** (401/403 con cuerpo del servidor → desloguea). Una terminal habilitada puede operar offline aunque el token del usuario haya vencido; al reconectar se reautentica y recién ahí se resuelven los pendientes.

### 8. Pago mixto en los ocho lectores

Tabla `pos_tenders` (creada con la operación). Las `SaleEntry` del grupo llevan `payment_method = "mixed"`: un valor *desconocido pero honesto* es menos peligroso que `"cash"` cuando la mitad fue crédito.

Lectura con **poblaciones disjuntas**: `fiado` = filas sin operación tendereada con `pm ∈ FIADO_METHODS` **+** tenders con `pm ∈ FIADO_METHODS`. Ídem `delayed`. Como `Σ tenders == Σ line_total` por construcción, el invariante `caja − inflows + fiado + delayed == ventas` cierra al centavo.

Los ocho lectores: `facts_service`, `facts_provider`, `cash_close_service`, `cash_service`, `business_state_service`, `api/v1/insights.py`, `transaction_repository.cash_breakdown_by_method` y **`transaction_repository.get_receivables_by_customer`** — este último filtra `== 'account'` y perdería la parte fiada de un mixto. **No alcanza con que "no exploten"**: hay que verificar el importe exacto en deuda del cliente, los cobros posteriores, la caja y la anulación.

**Declarado:** registrar `debit_card`/`credit_card`/`qr` **no procesa ni verifica el pago**. Con terminales externas, el cajero confirma que el cobro salió antes de cerrar el ticket.

### 9. Reconciliación: salidas pendientes, no faltantes

Offline el cajero ve el stock de IndexedDB descontado localmente, rotulado *"stock estimado — última sinc: HH:MM"*, y **nunca bloquea la venta**.

Al sincronizar, `on_insufficient_stock: "reject" | "capture"` (default `"reject"`, comportamiento actual intacto). Solo el flush reintenta con `"capture"`, y únicamente ante el código estructurado **`INSUFFICIENT_STOCK`** — nunca interpretando un 400 cualquiera como faltante. Con `"capture"`: las líneas que alcanzan descuentan normal; las que no persisten la venta igual sin descontar stock y generan una discrepancia.

**La discrepancia registra la salida pendiente completa, no el faltante.** Si había 3 y se vendieron 5 sin descontar ninguna, quedan **5 unidades de salida pendientes**; registrar un ajuste de entrada por 2 dejaría el stock mal. Resoluciones, todas con efecto definido, motivo, auditoría e idempotencia:

- `register_shrinkage` — el faltante era merma: descuenta lo que hay y registra la pérdida.
- `register_found_stock` — el stock estaba mal contado: ajuste de entrada y después la salida completa.
- `void_operation` — la venta no existió: anula el ticket entero.

**No hay `dismiss`.** Ocultar un aviso no reconcilia nada. La política aplica igual a una venta ya cobrada online o offline.

El invariante *stock negativo prohibido* no se toca. Es la **segunda** excepción a "toda venta viva descuenta" (la primera son las importadas con `source_upload_id`), así que exige bullet en `CLAUDE.md` en el mismo commit y un test que **enumere** las excepciones permitidas.

### 10. Arqueo por sesión de caja

**`pos_cash_sessions`**: el cajero abre con un fondo, vende, y cierra contando lo suyo. Resuelve turnos, que el `UniqueConstraint(tenant_id, close_date)` de `cash_closes` (`models/cash_close.py:54`) hace imposibles hoy.

**Las ventas offline que llegan después de un cierre no lo corrigen solas.** Quedan en una conciliación posterior visible sobre la sesión ya firmada. Un arqueo que se modifica retroactivamente no es un arqueo.

### 11. Impresión: medir, no asumir

`size: 80mm auto` no es válido. El tamaño y la paginación se **miden contra el driver elegido en el piloto** y el valor queda en configuración, no clavado en el CSS.

El iframe aísla estilos, pero `window.print()` **no confirma que salió papel** ni garantiza no bloquear. Por eso la venta se persiste y se confirma **antes** de imprimir, y la operación **siempre** se puede reimprimir (`GET /pos/operations/{id}/receipt`). Una impresión fallida nunca pone en duda un cobro.

`skipWaiting + clients.claim` puede mezclar una página vieja con un service worker nuevo. La versión nueva se activa en un **momento seguro** (carrito vacío, sin pendientes en vuelo), conservando los recursos de las operaciones en curso, con aviso al cajero.

---

## Bloques

Orden: **contrato y cantidades → persistencia e idempotencia → permisos y sesión → caja y pagos → reconciliación → hardware y piloto.** La documentación va en el commit de su bloque.

### B0 — Deuda previa bloqueante · sin migración
`api/v1/sales.py`, `schemas/transaction.py`, `features/ingestion/useOfflineSubmit.ts`.
- Persistir `unit_price` en `create_sale` y `manual-batch`.
- `date.today()` → `today_ar()` en los 7 validadores de fecha. El server corre en UTC, 3 h **adelante** de AR: entre las 21:00 y la medianoche argentina `date.today()` ya devuelve mañana, así que el validador **acepta una venta fechada mañana**. Es permisivo de más, no restrictivo de más — deja entrar justo lo que `MOTIVO_FECHA_FUTURA` existe para impedir, y una caja vende en esa franja todas las noches.
- `trigger_score_recalculation.delay(...)` → `trigger_score_recalculation_after_commit` (`sales.py:398`): sacar el análisis del camino crítico del cobro.
- El 400 de stock pasa a `INSUFFICIENT_STOCK` estructurado con producto, pedido y disponible.
- **Dejar de borrar por reintentos**: estado `FAILED` terminal y visible, nunca `remove`.
*Test:* la fila tiene `unit_price`; venta a las 23:50 ART no es futura; con el broker caído el `POST` responde 201; un ítem que falla 6 veces sigue en la cola.

### B1 — Spike: arranque sin red · resultado, no código que se conserva
PWA mínima (`manifest.json` + `sw.js` a mano + `/pos` estática), **reinicio real de la PC sin red**, no devtools. Mide también: cuánto tarda el primer arranque frío, si Chrome conserva IndexedDB tras `storage.persist()`, y si el driver de la térmica pagina un `@page` de 80mm. **Antes de construir la interfaz.**

### B2 — Idempotencia recuperable · migración `20260921_0001`
Tabla `idempotency_records`. Toca `services/idempotency.py` (API nueva que devuelve `Claimed | Replay(response) | Conflict`), y las rutas POS.
*Test:* replay con mismo hash → 200 con la respuesta original byte a byte; hash distinto → 409 `IDEMPOTENCY_KEY_REUSED`; concurrencia real de dos requests con la misma key; que el `response_json` se escribe en la misma transacción que la entidad.

### B3 — Contrato de operación, unidades y descuentos · migración `20260921_0002`
Tablas `pos_operations`, `pos_operation_lines`, `pos_tenders`. Columnas: `sales_entries.created_by_user_id`, `products.sale_unit`, `products.base_units_per_sale_unit`. Aditivas, idempotentes (convención E8c, espejo de `20260919_0001`), **sin backfill** — default `unit`/1 deja el histórico intacto.
Crea `domain/pos_operation.py` (cálculo puro de líneas, descuentos y reparto de centavos), `api/v1/pos.py`. Toca `schemas/product.py` (+`sale_unit`, `stock_display`).
*Test:* unitario del reparto — **el caso $1 sobre 3×$100 tiene que cerrar exacto**; `Σ tenders ≠ Σ line_total` → 422; el backend recalcula e ignora un `line_total` mentido por el cliente; producto en gramos con factor 1000 rinde `stock_display` correcto y aritmética intacta.

### B4 — Scan estricto y aprendizaje · sin migración
Crea `domain/scan_code.py` (clasificador puro). Toca `repositories/product_repository.py`, `api/v1/products.py` (`GET /products/lookup`, `POST /products/{id}/barcode` envuelto en `product_identity_guard`), `schemas/product.py`.
`POST .../barcode` **solo agrega, nunca pisa** (`409 BARCODE_ALREADY_SET`); endpoint propio y no `PATCH` porque `PATCH` lleva `require_modify_access` (un 428 `PIN_REQUIRED` en medio de un flush es intolerable) y no acepta `Idempotency-Key`.
*Test:* clasificador (VKT-, EAN13 con y sin checksum válido, EAN8, prefijo de balanza, alfanumérico con 8 dígitos adentro, `\r` del wedge); **`SCAN_AMBIGUOUS` cuando el GTIN de A es el SKU opaco de B**; los dos 409 de identidad; aprendizaje `PENDING` que al sincronizar encuentra el código ya tomado → revisión, sin tocar ventas anteriores.

### B5 — Rol `CASHIER`, catálogo POS y permisos · sin migración
`domain/user.py`, `schemas/user.py` (regex), `api/v1/deps.py`. **`PosProductResponse` propio** (`GET /pos/catalog`) sin `unit_cost_ars`, `margin_pct` ni `list_price_ars`. Permisos explícitos: descuento, cambio de precio, fiado, aprendizaje de códigos, anulación, resolución de discrepancias.
*Test:* matriz completa de rutas permitidas/prohibidas; y un test que **asserta que la respuesta del catálogo POS no contiene las claves de costo** — que la pantalla no las muestre no alcanza.

### B6 — Terminal habilitada y sesión offline · migración `20260921_0003`
Tabla `pos_terminals`. Toca `api/v1/pos.py` (enrolar, listar, deshabilitar), `lib/api.ts` (distinguir red de sesión revocada; **no `logout()` ante fallo de red en el refresh**), `stores/authStore.ts`.
*Test:* refresh que falla por red → no desloguea y reintenta; refresh que falla con 401 del servidor → desloguea; terminal deshabilitada → sus pendientes quedan `REQUIERE_AUTENTICACION` y no se sincronizan.

### B7 — Persistencia offline en IndexedDB · solo frontend
Crea `lib/offline/posDb.ts` (operaciones, pagos, pendientes y efecto de stock en **una** transacción), `lib/offline/catalogDb.ts`, `lib/offline/tabLock.ts` (Web Locks), `features/pos/usePosSync.ts`, `features/pos/PendingOperationsPanel.tsx` (con exportación). El `sw.js` de B1 se formaliza: `/_next/static/**` cache-first, documento de `/pos` network-first con fallback, **bypass total** para RSC y para `NEXT_PUBLIC_API_URL`, activación en momento seguro.
*Test:* jest + `fake-indexeddb` — la operación existe en IndexedDB **antes** de que se dispare el primer `POST`; matar el proceso entre la escritura y el envío y recuperar al abrir; pendiente de otro tenant/terminal no se sincroniza; dos pestañas no escriben a la vez; `logout()` no borra pendientes.

### B8 — Sesiones de caja y arqueo · migración `20260921_0004`
Tabla `pos_cash_sessions`. Toca `cash_close_service.py`, `api/v1/cash_closes.py`, `api/v1/pos.py`.
*Test:* dos cajeros cierran su sesión el mismo día; una venta offline que llega tras el cierre **no modifica** el arqueo firmado y aparece en la conciliación posterior.

### B9 — Pago mixto en los ocho lectores
Toca `facts_provider.py`, `facts_service.py` (`_split_liquidez`), `cash_close_service.py`, `cash_service.py`, `business_state_service.py`, `api/v1/insights.py`, `transaction_repository.py` (`cash_breakdown_by_method` **y `get_receivables_by_customer`**).
*Test:* **obligatorio por la regla 2c de `CLAUDE.md`** — caso nuevo en `app/tests/application/test_facts_reconciliation.py` con el invariante sobre una muestra con mixtos. Más: **el importe exacto en deuda del cliente** tras un mixto con parte fiada, el cobro posterior de esa deuda, la caja del día, y la anulación; un día con venta mixta espera solo la parte de efectivo en el arqueo.

### B10 — Anulación de ticket
Toca `api/v1/pos.py` (`POST /pos/operations/{id}/void`), `api/v1/sales.py` (PATCH/DELETE → 409 `BELONGS_TO_POS_OPERATION`), `stock_service.py` (reversa por línea vía `revert_sale_stock`).
*Test:* anular revierte líneas, tenders y stock de forma atómica y auditada; el PATCH genérico sobre una línea de operación da 409; anular dos veces es idempotente.

### B11 — Reconciliación de stock · migración `20260921_0005`
Tabla `pos_stock_discrepancies` (con **salida pendiente**, no faltante). Toca `stock_service.py` (`try_decrement_for_sale`, **sin tocar `decrement_for_sale`**), `api/v1/pos.py` (listar y resolver), `schemas/transaction.py` (`on_insufficient_stock`).
*Test:* 3 en stock y venta de 5 → discrepancia de **5 pendientes**, stock intacto, venta completa; cada resolución con su efecto exacto; resolver dos veces es idempotente; un `reject` normal sigue dando `INSUFFICIENT_STOCK`; `decrement_for_sale` sin cambios. Más el test que **enumera las excepciones permitidas** a "toda venta viva descuenta stock".

### B12 — Ticket, etiquetas y reimpresión · solo frontend + 1 endpoint
Crea `lib/print/types.ts` (`interface ReceiptPrinter`), `lib/print/browserPrinter.ts` (iframe), `lib/print/escpos.ts` (stub `NotImplemented`, para probar la interfaz contra dos implementaciones), `features/pos/TicketTemplate.tsx`, `features/pos/LabelSheet.tsx`, `lib/barcode/code128.ts`, `styles/print.css`. Backend: `GET /pos/operations/{id}/receipt`.
El `@page` sale de configuración, medida en B1. El ticket dice **"Comprobante no fiscal"**. La operación se confirma antes de imprimir y siempre se puede reimprimir.
*Test:* `code128` contra patrones conocidos **incluido el checksum**; snapshot del ticket con mixto, descuento y vuelto; `browserPrinter` limpia el iframe aunque `print()` tire; reimprimir no crea otra venta.

### B13 — Pantalla de caja
`app/(protected)/pos/page.tsx`, `features/pos/{PosScreen,CartTable,ScanInput,PaymentPanel,DiscountControl,CashSessionBar}.tsx`, `features/pos/useScanner.ts`.
`useScanner`: `keydown` global, ventana de ~30 ms entre teclas, terminador **configurable** (hay scanners que emiten Tab), desactivado con foco en texto libre. Sin dependencias.
*Test:* jest de `useScanner` (ráfaga vs tipeo humano vs input enfocado); RTL del flujo scan → carrito → descuento → mixto → cobro.

### B14 — Sacar la caja de la compuerta de suscripción
`api/v1/deps.py`, `api/v1/{sales,cash_closes,pos}.py`, `app/tests/api/v1/test_subscription_write_gate.py`, `CLAUDE.md`.
Se le saca el gate a las rutas de caja; dependency nueva `require_active_subscription_for_insights` (402 `SUBSCRIPTION_INSIGHTS_LOCKED`) sobre los `GET` de dashboard/insights/forecast/health-scores/economic-summary/momentum/export. `/chat` ya está bloqueado vía `reserve()` — verificar con test, no tocar. La compuerta estructural pasa a **tres** conjuntos, incluido `RUTAS_DE_CAJA_SIN_GATE` como tripwire.
*Test:* en `READ_ONLY`, `POST /pos/operations` → 201 y `GET /insights/current` → 402; los tres estructurales; el agente **sigue** bloqueado para `REGISTER_SALE` (eso es chat, no caja).

### B15 — Runbook y piloto
`docs/runbooks/pos.md`: scanner probado y su sufijo, acceso directo con `--kiosk-printing`, `@page` medido, qué significa "stock estimado", cómo resolver cada tipo de discrepancia, cómo exportar pendientes, y qué hacer si el corte supera la vida de la credencial de terminal.

---

## Riesgos, ordenados por daño

| # | Riesgo | Mitigación |
|---|---|---|
| 1 | Venta cobrada que se pierde (cierre del navegador durante el envío, o borrado por reintentos) | B0 + B7: escribir en IndexedDB **antes** de enviar; nunca borrar |
| 2 | Doble cobro por respuesta perdida | B2: idempotencia que devuelve la operación original |
| 3 | Doble conteo de caja con mixto, o deuda de cliente que desaparece | B9: poblaciones disjuntas + los **ocho** lectores con importes exactos |
| 4 | Scan que cobra el producto equivocado | B4: clasificación estricta y `SCAN_AMBIGUOUS` en vez de elegir |
| 5 | Cajero deslogueado en medio de un corte | B6: terminal habilitada, red ≠ sesión revocada |
| 6 | `stock_units` en unidades base malinterpretado por un lector que lo muestra | B3: `sale_unit` + `stock_display`; auditar las superficies que renderizan stock |
| 7 | SW nuevo sobre página vieja → caja inconsistente | B7: activación en momento seguro, no `skipWaiting` a secas |
| 8 | Reconciliación que deja el stock peor (ajuste por el faltante en vez de la salida) | B11: salida pendiente completa, sin `dismiss` |
| 9 | El cajero recibe costos y márgenes en el catálogo offline | B5: schema propio + test que asserta ausencia de claves |
| 10 | Abrir la 2ª excepción a "toda venta viva descuenta" | Bullet en `CLAUDE.md` + test que enumera las excepciones |

---

## Verificación

**Por bloque:** `make test-file FILE=<el del bloque>`; frontend `npm run test`.

**Antes de cada push con migración** (B2, B3, B6, B8, B11): `pytest -m postgres -n 0` contra `vektor_pgtest` — la suite SQLite saltea la compuerta de migraciones idempotentes y CI no la ve.

**Antes de cada PR:** `make check` y `make test-cov` (≥60%, la misma compuerta que CI); frontend `npm run type-check && npm run lint && npm run test && npm run build`. **Nunca `ruff format` ni `make format`.**

**Smoke end-to-end con hardware real, antes de cerrar B15:**
1. Escanear un EAN desconocido → aprendizaje → el segundo scan lo resuelve directo.
2. Un GTIN que coincide con el SKU opaco de otro producto → `SCAN_AMBIGUOUS`, no elige.
3. Carrito de 3 líneas, **descuento indivisible al centavo**, cobro mixto efectivo + débito con vuelto → el ticket sale y los tenders imputan cada parte a su método.
4. Vender 0,750 kg de un producto en gramos → el ticket dice 0,750 kg y el stock baja 750.
5. Imprimir una etiqueta con `internal_sku` y escanearla con el mismo lector.
6. **Cerrar el navegador durante el cobro** → al reabrir, la operación está y se puede sincronizar.
7. **Respuesta perdida después del commit** (matar la red justo después del POST) → el reintento devuelve el mismo ticket, no crea otro.
8. **Cortar internet, reiniciar la PC, abrir `/pos`** → abre y vende. Reconectar: las ventas suben una sola vez.
9. Vender offline más unidades de las que hay → la venta entra completa, la discrepancia dice la salida pendiente, y cada resolución deja el stock correcto.
10. **Cambio de usuario con pendientes** → los pendientes del cajero anterior no se sincronizan bajo la nueva sesión.
11. **Ventas offline que llegan después del arqueo** → el cierre no se modifica y aparece la conciliación.
12. Anular un ticket de 3 líneas → revierte líneas, pagos y stock; el PATCH genérico sobre una de esas ventas da 409.
13. Tenant en `READ_ONLY`: la caja vende, el dashboard da 402.
14. Usuario `CASHIER`: ve la caja y nada más; el catálogo que recibe no trae costos.
