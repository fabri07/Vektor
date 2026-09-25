# B12 — Ticket, etiquetas y reimpresión

## Contexto
B13 dejó la caja cobrando, pero no sale papel: el cliente se va sin comprobante y los productos sin código de fábrica (decoración, limpieza) no se pueden escanear. Según la tabla del usuario, B12 es lo último para la primera prueba con impresora. Plan maestro: `docs/plans/vektor-pos-scanner-y-ticket.md` §B12. Decisión ya tomada allí: `window.print()` en iframe + Chrome `--kiosk-printing`, cero `.exe`; el ticket dice **"Comprobante no fiscal"**; **la venta se confirma antes de imprimir** y siempre se puede reimprimir.

## Backend (sin migración)
1. **`GET /pos/operations/{id}/receipt`** → `PosReceiptResponse`: nombre del negocio (`tenants.display_name` — el cajero no puede leer `/tenants/me`), fecha, número corto (primeros 8 del id; **no hay numeración correlativa**: es no fiscal, declarado), líneas con **nombre del producto** (join; si el producto se borró, "Producto eliminado"), cantidad + unidad, precio, descuento de línea + parte del global, total; subtotal, descuento, total; pagos con su medio; efectivo recibido y vuelto; cliente si es fiado; nombre del cajero y de la caja; `status` (un ticket anulado se reimprime con "ANULADO"). Sin costos.
2. **`GET /pos/operations?limit=20`** → los últimos tickets (id, fecha, total, estado) para "Reimprimir". Sin esto, recargar la página dejaba sin forma de reimprimir.
3. **Autorización:** mismos roles que cobrar. Un **cajero sólo ve sus propios tickets** (espejo de `VOID_NOT_OWN_TICKET`: no ve cuánto vende otro cajero); OWNER/ADMIN ven todos. Otro tenant → 404. Las dos rutas entran a `CASHIER_ALLOWED_ROUTES` (el barrido de `test_cashier_access.py` las cubre solo).

## Frontend
- **`lib/print/types.ts`** (`interface ReceiptPrinter { print(html, pageCss) }`) + **`lib/print/browserPrinter.ts`**: iframe oculto, escribe el HTML, espera `load`, `contentWindow.print()`, y **quita el iframe en `finally` aunque `print()` tire**. Un fallo de impresión nunca pone en duda un cobro: se muestra "No se pudo imprimir — Reimprimir".
- **`features/pos/TicketTemplate.tsx`**: render estático (`renderToStaticMarkup`) del recibo, monoespaciado, ancho del papel. **`styles/print.css`** embebido en el iframe.
- **Config por PC** (`posPrintConfigStore`, junto al del lector): papel **80 mm / 58 mm**, el `@page` con los tres presets del spike B1 (A `80mm 297mm` · B `72mm 200mm` · C "manda el driver"; default A hasta que B1 mida cuál funciona) e **"Imprimir al cobrar"** (default sí).
- **En la caja:** al cobrar → pide el recibo al servidor e imprime; botón "Reimprimir" en la venta cobrada; panel **"Últimos tickets"** con reimprimir (y anular si corresponde, reusando `VoidLastTicket` generalizado a "anular ticket").
- **Etiquetas** (`features/products/LabelSheet.tsx` + **`lib/barcode/code128.ts`**): en `/products`, "Imprimir etiquetas" abre un modal para elegir productos y copias; cada etiqueta = nombre, precio (opcional) y **Code128 del `internal_sku`** (`VKT-…`) en SVG. Dos formatos: **hoja A4** (3×8, adhesivas comunes) y **rollo de la térmica** (una por fila), porque muchos negocios sólo van a tener la térmica. Productos sin `internal_sku` se saltean con aviso. Code128-B con checksum mod 103, sin dependencias.

## Cambio respecto del plan maestro
- **Se saca el stub `escpos.ts`**: existía "para probar la interfaz contra dos implementaciones", pero una implementación que sólo tira `NotImplemented` no prueba nada y es código muerto. La interfaz queda; ESC/POS se escribe cuando exista (fuera de v1).

## Declarado fuera de alcance (CLAUDE.md)
Numeración correlativa y datos fiscales (CUIT/dirección no existen en el perfil; es no fiscal), apertura de cajón (lo hace el driver: "abrir cajón al imprimir"), confirmar que salió papel (el navegador no lo sabe), impresión offline (B7), el `@page` definitivo (lo mide B1 en el hardware).

## Archivos
- Backend: `api/v1/pos.py` (2 rutas), `schemas/pos.py` (`PosReceiptResponse`, `PosOperationSummary`), `domain/pos_permissions.py` (allowlist), tests en `test_pos_operations.py` (o `test_pos_receipt.py`) + barrido de cajero.
- Frontend: `lib/print/{types,browserPrinter}.ts`, `lib/barcode/code128.ts`, `features/pos/{TicketTemplate,RecentTickets}.tsx`, `features/products/LabelSheet.tsx`, `stores/posPrintConfigStore.ts`, `styles/print.css`; tocar `PosScreen.tsx`, `VoidLastTicket.tsx`, `pos.service.ts`, `app/(protected)/products/page.tsx`.
- `CLAUDE.md` + `docs/plans/b12-ticket-y-etiquetas.md` en el mismo commit.

## Verificación
- **pytest:** recibo con nombres, pagos y vuelto; sin claves de costo; ticket anulado dice `VOIDED`; cajero no ve el ticket de otro cajero (404) y sí el suyo; otro tenant 404; el listado del cajero sólo trae los suyos; reimprimir (GET) no crea venta ni audita cobro.
- **jest:** `code128` contra patrones conocidos (start B, stop, valores de tabla) **y el checksum** calculado a mano; snapshot del ticket con descuento, vuelto y dos pagos (el template ya soporta mixto para B9); `browserPrinter` quita el iframe aunque `print()` tire; en la caja, imprimir falla → la venta sigue cobrada y aparece "Reimprimir"; "Imprimir al cobrar" apagado no imprime.
- **Mutation:** el `finally` del iframe, el filtro de tickets propios del cajero, el checksum.
- Suite backend completa + ruff + mypy; frontend tsc, lint, jest, build — todo con exit code capturado. Sin migración. En `feat/pos-b5-cajero`, sin pushear.

## Cómo quedó (diferencias con este plan)
- El recibo trae por línea `gross_ars` + `discount_line_ars` (no un `discount_ars` que sumaba la parte del global): así las líneas suman el subtotal impreso. Lo mostró el snapshot del primer borrador.
- El template es una función pura (`features/pos/ticketTemplate.ts`) y no un componente con `renderToStaticMarkup`, para escapar todo en un solo lugar; el CSS va como string (`ticketCss`, `labelsCss`) en vez de `styles/print.css`, porque se inyecta en el iframe.
- En el rollo, las etiquetas usan el mismo `@page` que el ticket de esa PC (`size: 80mm auto` no es CSS válido).
- Las etiquetas buscan productos con `/pos/catalog?q=` (trae `internal_sku` y precio, sin costos).
