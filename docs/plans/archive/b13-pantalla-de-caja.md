# B13 — Pantalla de caja (revisado)

## Contexto

B0–B6 dejaron el servidor de la caja completo (`POST /pos/operations`, `/products/lookup`, aprendizaje de códigos, cajero, terminales, anulación) pero no hay pantalla: un cajero ve `CashierHome` ("todavía no está disponible"). Según la tabla de avance del usuario, B13 + B12 son lo que falta para la primera prueba real. Este plan es la versión revisada del que se presentó: la revisión contra el código encontró 10 fallas del borrador.

## Fallas del borrador y cómo se corrigen

1. **Reintento con cuerpo distinto → `IDEMPOTENCY_KEY_REUSED`.** La huella es sobre el cuerpo validado; si el reintento vuelve a calcular `operation_date = ahora`, el hash cambia y el servidor responde 409 aunque la venta exista. **Se congela el payload COMPLETO** (id + fecha + líneas + pagos) al apretar Cobrar; el reintento reenvía exactamente ese objeto. `operation_date` en hora local sin zona (misma convención que B0).
2. **Recargar la página perdía la venta "en duda"** → el cajero la vuelve a cobrar = doble cobro. La operación en duda (payload congelado) se guarda en `localStorage` (clave propia, sobrevive a `logout`) hasta que se resuelve; al abrir la caja, si existe, lo primero es reintentarla. Es un puente mínimo hasta la cola IndexedDB de B7.
3. **El Enter del scanner "aprieta" el botón con foco.** Si el foco quedó en "Cobrar", una lectura dispara el cobro. El terminador de una ráfaga se consume con `preventDefault` en fase de captura, y los botones de la caja devuelven el foco al scanner después de usarse.
4. **"Desactivado con foco en un input" rompe el caso real**: con el foco en "efectivo recibido", un escaneo mete dígitos en el importe (vuelto absurdo). Los inputs numéricos de la caja se marcan `data-pos-scan="capture"`: si llega una ráfaga ahí, se restaura el valor previo al primer carácter de la ráfaga y se procesa como lectura. Sólo el buscador por nombre (modal) desactiva el scanner.
5. **Cambio de precio entre agregar y cobrar → 422 `TENDERS_MISMATCH` en prosa.** El pago lo manda el cliente con el total de su vista previa; si el dueño cambió un precio, el servidor rechaza con un texto. **Único cambio de backend:** `TENDERS_MISMATCH` agrega `expected_total_ars` y `lines: [{product_id, unit_price_list}]` (el cálculo ya existe en `calcular_operacion`). La caja actualiza precios, muestra qué cambió y pide reconfirmar. Sin esto la venta queda trabada sin explicación.
6. **Bajar el catálogo entero era innecesario**: `/pos/catalog` ya acepta `q`. La búsqueda por nombre consulta `?q=` con debounce; nada de caché que se ponga vieja (la descarga completa es de B7, para offline).
7. **La suscripción vencida bloquea la caja (402)** hasta B14: mensaje propio ("la suscripción está vencida; la caja vuelve con B14/al regularizar") en vez de un error genérico.
8. **Un ticket mal cobrado no tiene arreglo**: no hay UI de anulación en ningún lado, así que en la primera prueba un error sólo se corrige por API. Se agrega **"Anular último ticket"** (el de esta caja, con motivo), con los permisos de B5: requiere PIN → el `PinGateModal` se monta también en la caja (428 lo abre el interceptor).
9. **Umbral de 30 ms fijo**: lectores Bluetooth/configurados emiten más lento. Umbral y terminador (Enter/Tab) configurables **por PC** en `localStorage`, con mínimo de 4 caracteres para que dos teclas rápidas de una persona no sean una lectura.
10. **Cajero en PC no habilitada se enteraba al cobrar.** Se chequea al entrar: sin terminal del tenant (o marcada inválida) → pantalla de bloqueo antes de armar un carrito.

## Diseño final

**Ruta:** `app/(protected)/pos/page.tsx`. `layout.tsx`: en `/pos` renderiza sin sidebar/header/chat (pero con `PinGateModal`); un CASHIER en cualquier otra ruta se redirige a `/pos`. `CashierHome` queda como pantalla de "PC no habilitada". Link "Caja" en la sidebar para OWNER/ADMIN.

**Estados de la venta** (`lib/pos/cart.ts`, puro): `editando → enviando → (cobrada | rechazada | en_duda)`.
- `rechazada` (4xx definitivo: stock, sin precio, permiso, mismatch) → el servidor revirtió todo; el carrito se desbloquea y el próximo cobro usa id nuevo.
- `en_duda` (red, timeout de 15 s de `api.ts`, 5xx) → carrito bloqueado, "Reintentar" con el mismo payload; "Descartar" sólo con confirmación explícita ("puede haberse cobrado").
- Botón deshabilitado mientras `enviando` (doble clic).

**Scan:** `useScanner` (captura global, ráfaga, terminador consumido) → `GET /products/lookup`: 1 → al carrito (repetido suma 1); `SCAN_AMBIGUOUS` → elegir entre `candidates`; `SCAN_NOT_FOUND` + `learnable` + permiso `learn_barcode` → buscar por nombre y `POST /products/{id}/barcode`, luego al carrito; sin permiso → "avisale al encargado", mostrando el **código crudo** leído (diagnostica un lector con distribución de teclado distinta, p. ej. `VKT'` en vez de `VKT-`); balanza → no soportado.

**Carrito:** cantidad, quitar, stock disponible visible con aviso si la cantidad lo supera (el rechazo real sigue siendo el `INSUFFICIENT_STOCK` del servidor). Descuento por línea y global sólo con permiso `discount` (de `user.pos_permissions`, que `/auth/me` ya devuelve efectivo). Vista previa = misma fórmula que el servidor (precio × cantidad / factor, half-up una vez por línea); después de cobrar se muestra lo que devolvió el servidor.

**Pago:** un medio (mixto = B9). Efectivo: recibido + vuelto, validado ≥ total antes de enviar. Fiado: permiso `fiado` + cliente de `/pos/customers?q=`.

**Errores con copy propio:** `INSUFFICIENT_STOCK` (producto + disponible), `PRODUCT_WITHOUT_PRICE`, `TENDERS_MISMATCH` (con precios nuevos), `DISCOUNT_EXCEEDS_TOTAL`, `CASH_RECEIVED_TOO_LOW`, `TERMINAL_*`, 402, 403 de permiso, `IDEMPOTENCY_KEY_REUSED` (no debería ocurrir con el payload congelado: se muestra como error interno con el id, para diagnóstico).

## Fuera de alcance (declarado en CLAUDE.md)
`CashSessionBar` (B8), mixto (B9), impresión (B12), venta offline y cola (B7; hasta entonces sin red no se cobra, pero la venta en duda no se pierde), dos pestañas a la vez (Web Locks, B7), venta por peso (la cantidad se carga en unidades base), venta con 100 % de descuento (el servidor exige pago > 0), reloj de la PC adelantado (el servidor rechaza fecha futura: se muestra el mensaje).

## Archivos
- Nuevos: `app/(protected)/pos/page.tsx`; `features/pos/{PosScreen,CartTable,ScanInput,PaymentPanel,DiscountControl,ProductPicker,VoidLastTicket}.tsx`; `features/pos/useScanner.ts`; `lib/pos/cart.ts`; `lib/pos/pendingOperation.ts` (la venta en duda en `localStorage`); `stores/posScannerConfigStore.ts` (umbral/terminador por PC); `lib/pos/errors.ts`.
- A tocar: `services/pos.service.ts` (catálogo `q`, lookup, operación, anulación, clientes, barcode); `app/(protected)/layout.tsx`; `components/layout/Sidebar.tsx`; `features/pos/CashierHome.tsx`.
- Backend (como quedó): `api/v1/pos.py` arma el 422 `TENDERS_MISMATCH` con `expected_total_ars` y los precios por línea desde la `OperacionCalculada` que ya tiene (el dominio no cambió); `api/v1/products.py` agrega a cada candidato de `SCAN_AMBIGUOUS` la vista de caja del producto (`PosProductResponse`, sin costos) — sin ella, después de elegir, la caja no tenía el precio, y el cajero no puede leer `/products/{id}`. Tests en `test_pos_operations.py` y `test_product_scan.py`.
- `CLAUDE.md` en el mismo commit. Plan persistido en `docs/plans/b13-pantalla-de-caja.md`.

## Verificación
- **Backend:** `TENDERS_MISMATCH` trae `expected_total_ars` y precios actuales tras cambiar un precio; suite completa (`pytest`, ruff, mypy) con exit code capturado (sin `| tail`). Sin migración → no hace falta `-m postgres`, se corre igual por ser rama con migraciones pendientes.
- **Jest `useScanner`:** ráfaga = lectura; tipeo humano no; mínimo de 4; Tab consumido; ráfaga dentro de un input `capture` restaura su valor; Enter terminador con foco en un botón NO lo activa; buscador enfocado lo ignora.
- **Unitarios `cart.ts`:** $1.234,56/kg × 750 g = $925,92; escaneo repetido suma; transiciones de estado (4xx desbloquea con id nuevo, red deja `en_duda`).
- **RTL del flujo:** scan → carrito → descuento → efectivo con vuelto → cobrada; reintento manda el payload **idéntico** (incluida la fecha); recargar con una venta en duda la reintenta primero; mismatch actualiza precios; aprender código; ambiguo; cajero sin terminal bloqueado; sin permiso no hay descuento; anular último ticket pasa por PIN.
- **Mutation tests:** payload congelado, restauración del input `capture`, consumo del terminador, persistencia de la venta en duda.
- Frontend: `tsc --noEmit`, lint, jest, `next build`, cada uno con `echo $?`.
- Todo en `feat/pos-b5-cajero`, sin pushear.
