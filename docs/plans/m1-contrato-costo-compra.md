# M·1 — contrato del costo de compra (mini-plan)

Fecha: 2026-10-10. Paso 4 de la fase 2 de `docs/plans/cierre-ingesta-capital-margen.md` (v3). Este documento baja ese punto al código actual; no cambia sus decisiones. El OK de la fase 2 (mergear con verde) cubre la ejecución salvo las **decisiones abiertas** de abajo, que son del dueño.

## Qué hay hoy (verificado contra `main` 2026-10-10)

- **No existe una tabla de compras.** Una compra deja dos filas sin FK entre sí: un `expense_entries` (`COGS`/`INVENTORY`, `amount` total de la línea) y un `inventory_movements` (`movement_type='purchase'`, `qty`, `unit_cost` = precio **facturado**, `occurred_at` = fecha de negocio, `voided_at`). El número de comprobante sólo se usa para agrupar el flete; no queda guardado.
- **`products.unit_cost_ars` lo pisa el último que escribe**, por fecha de proceso y no de negocio. Escriben 10 rutas: `stock_service.increment_stock` (compra manual, resolve-purchase de Otros, `REGISTER_PURCHASE` y `UPDATE_STOCK` del chat), `_apply_purchase_to_stock` (ingesta y remito de proveedor), los dos caminos del import de catálogo, el dedup y el PATCH manual.
  - La protección con flete / sin flete (`debe_pisar_costo_de_referencia`) existe **sólo** en ingesta y remito.
  - `has_user_edits` **no protege el costo** en ninguna ruta.
- **Envío:** `_vektor_costo_base` en el producto; el flete del comprobante es un gasto `LOGISTICS` aparte (con `attributed_to_inventory` si se capitalizó). La compra manual no tiene campo de envío.
- **Impuesto:** no está modelado. Las claves `_vektor_impuestos`/`_vektor_descuento` de `purchase_cost.py` nunca se escriben. `taxes` sólo entra al costo con `monto_sin_ajustes` y con el tenant en el rollout `PURCHASE_COST_ROLLOUT_TENANT_IDS` (vacío = apagado para todos).
- **No hay historial de costo.** Lo único fechado es `inventory_movements.unit_cost`, que guarda lo facturado y no el costo final con flete.
- **El COGS de Facts usa el costo ACTUAL** (`facts_provider.py:84-115`). Eso es M·2, no M·1.

## Qué entrega M·1

Una sola respuesta a «¿cuál es el costo de referencia de este producto a la fecha F, y de dónde sale?», con el origen a la vista y sin inventar nada. M·1 no cambia todavía el margen que se muestra (M·2/M·3), salvo dejar de pisar el costo por orden de proceso.

## PRs (serie, cada uno con revisión + CI + merge)

### M·1a — contrato en el movimiento de compra + selección pura (sin cambio de comportamiento)
- **Migración aditiva e idempotente** sobre `inventory_movements`, colgada del head real. Columnas nullable:
  - `unit_cost_final` (el costo por unidad con los ajustes aplicados, que es lo comparable);
  - `shipping_treatment` (`incluido` | `adicional` | `no_aplica` | `desconocido`);
  - `tax_treatment` (`incluido` | `adicional` | `no_aplica` | `desconocido`), más `tax_rate` y `tax_amount`;
  - `tax_recoverable` (bool nullable: NULL = no confirmado);
  - `document_ref` (comprobante, texto);
  - `cost_contract_version` (smallint).
  - Si una columna es NULL, el dato es «desconocido, no confirmado». Los movimientos viejos quedan así: **no se marcan confirmados retroactivamente**.
- **Dominio puro** `domain/costo_de_referencia.py`:
  - `elegir_ultima_compra(candidatas, fecha_corte)` toma las compras con `voided_at IS NULL` y fecha de negocio `COALESCE(occurred_at, created_at) <= fecha_corte`.
  - **Desempate estable:** fecha de negocio desc → `created_at` desc → `id` desc.
  - **Precedencia (como quedó implementada):** `compra` → `stock_inicial` → `costo_cargado` → `sin_costo`, siempre con el origen. Un costo 0 de un producto incompleto da `sin_costo` (pendiente), nunca 0 confirmado.
    - `compra` es todo `purchase` vivo, incluida la mercadería que un catálogo declaró como compra (`purchase` + `catalog_initial_stock`, con COGS).
    - `stock_inicial` es el saldo de **apertura** valorizado (`adjustment` + `catalog_initial_stock`). Corrección de la revisión: la primera versión tomaba como «stock inicial» a la compra declarada.
    - `costo_cargado` es `products.unit_cost_ars` sin compra. Hoy no se puede distinguir si vino de una edición manual o del catálogo, por eso se marca como **estimación**. Separar `manual` de `catalogo` depende de las decisiones 2 y 3 y va en M·1c.
  - **Repositorio:** sólo existe la versión por lote, `costos_de_referencia(session, tenant_id, product_ids, corte)`; un solo producto es una lista de uno.
- **Repositorio:** `costo_de_referencia(session, tenant_id, product_id, fecha)`, con el tenant del JWT, más su versión por lote para varios productos.
- **Tests:**
  - compra atrasada (comprobante viejo cargado hoy);
  - empate de fecha;
  - anular la elegida → se vuelve a elegir otra;
  - compra posterior a la fecha de corte → no es elegible;
  - otro tenant;
  - el movimiento viejo sin contrato sale como `desconocido`;
  - `-m postgres` por la migración.

### M·1b — las rutas de compra escriben el contrato
- **Primero, la fecha.** `stock_service.increment_stock` y las rutas del chat guardan `occurred_at = datetime.now(UTC)` real, mientras que la ingesta guarda hora AR rotulada UTC. Una compra manual cargada de 21 a 24 AR cae en el día siguiente del corte. Se unifica a hora AR rotulada (`fecha_de_negocio()`) antes de M·1c. Lo encontró la revisión de M·1a.
- `increment_stock` y `_apply_purchase_to_stock` reciben el contrato y lo guardan en el movimiento.
- **Ingesta:** `unit_cost_final` sale de `build_line_costs` (motor existente, sin calculadora nueva). El envío se calcula al registrar y sobre el mismo comprobante. Con la decisión `monto_incluye` el envío queda `incluido` y el impuesto `desconocido`: **`monto_incluye` no prueba IVA**.
- **Compra manual / Otros / chat / remito:** lo que el usuario declara; lo no declarado queda `desconocido`.
- No se pide nada nuevo en pantalla todavía: los campos de envío e IVA en el formulario de compra van con M·3 / B·5.
- **Cómo quedó (2026-10-11):**
  - **Corrección al texto de arriba:** `monto_incluye` habla de descuento e impuestos, no del envío. El envío sale del motor: `incluido` si se capitalizó, `adicional` si había envío y quedó como gasto aparte, `desconocido` si la hoja no dice nada. El impuesto queda `desconocido` con `monto_incluye`, como decía el plan.
  - `UPDATE_STOCK` del chat y el stock de catálogo declarado compra quedan **sin** contrato (decisión 3: el catálogo no cuenta como compra confirmada).
  - `tax_amount` es por unidad.
  - **Hallazgos registrados, fuera de M·1b:** ventas, mermas y ajustes siguen con `now(UTC)` (lo necesita M·2); el camino de tabla suelta no lee el mapeo explícito a `unit_price` en compras.

### M·1c — el costo de referencia deja de ser «el último que escribió»

**Cómo se ejecuta (2026-10-11):** en dos PRs, por el riesgo de cascada (son ~16 escritores).
- **M·1c-1:** compras por `increment_stock` (manual, «Otros», chat) recalculan; ediciones manuales y de catálogo quedan guardadas con su fecha; undo devuelve también la procedencia. **La guarda con flete / sin flete se reemplaza por un rótulo** (decisión del dueño 2026-10-11): el costo es el de la última compra y `_vektor_costo_base` dice si incluye envío.
- **M·1c-2:** ingesta de compras y remito recalculan (sale la guarda vieja de `_apply_purchase_to_stock`); anulación y reversas (borrado, relectura) recalculan **después** de restaurar el `before` (la restauración sigue: devuelve la procedencia del costo, y el recálculo decide con las compras que quedan vivas). **Dedup:** sin cambios — completa el costo del canónico sólo si está vacío, y las claves `_` no viajan entre duplicados; el próximo recálculo del canónico decide con su propia procedencia.
- Después de registrar o anular una compra, `unit_cost_ars` = `costo_de_referencia(hoy)`. Una compra atrasada ya no pisa a una más nueva, y anular la elegida hace volver a la anterior.
- Se conserva la guarda con flete / sin flete en la comparación.
- Los dos caminos del import de catálogo, el dedup y el PATCH manual escriben un costo de origen `catalogo` / `manual`, con fecha. Su precedencia frente a una compra es la **decisión abierta 2**.
- Snapshots y undo: la reversa de relectura y de borrado (F11) restaura `unit_cost_ars` recalculando la referencia, no con `setattr` del valor viejo.

### M·1d — régimen y base comparable (sólo el contrato; el margen lo usa M·2)
- `comparable_unit_cost(costo, regimen)`:
  - **RI con `tax_recoverable=true` confirmado:** neto. Un IVA `incluido` se extrae por importe o por base y tasa confirmados, una sola vez.
  - **Otros casos:** el IVA no recuperable confirmado integra el costo.
  - **Base desconocida:** sale con un motivo y queda fuera del margen confirmado.
  - Nunca 21% por defecto; `no_aplica` ≠ `adicional`; el alias `registered` no habilita el cálculo confirmado.
- `domain/fiscal_condition.py` y `docs/claude/plataforma.md`: el régimen pasa de «sólo informativo» a «guía la base comparable del margen».
- **Tests:** los ejemplos aprobados del plan.
  - 10 unidades, $1.000 de mercadería + $100 de envío + $210 de impuesto no recuperable → **$131 por unidad**; si ya venían incluidos, no se suman.
  - RI: $605 con IVA 21% recuperable → base **$500**.
  - Además: `no_aplica` y tasas distintas por componente.

## Decisiones del dueño (cerradas el 2026-10-10)

1. **Dónde vive el contrato — RESUELTA: columnas** (se aplicó la recomendación en M·1a). Planteo original: columnas en `inventory_movements` (lo que propongo: es la fila que ya tiene fecha de negocio, anulación y procedencia) o una tabla aparte 1:1. Recomiendo columnas.
2. **Costo manual frente a compra — APROBADA la propuesta:**
   - **Propuesta (aprobada):** la edición manual gana hasta que llega una compra con fecha de negocio **posterior** a la edición. Una compra atrasada no pisa una edición más nueva.
   - **Alternativa:** que lo manual gane siempre hasta que el usuario lo suelte. Esto obliga a agregar un control en la pantalla.
3. **Costo de catálogo — APROBADA la propuesta** (hoja de precios, sin compra): entra como origen `catalogo`, con la fecha de la carga, y una compra posterior lo reemplaza. **No cuenta como compra para el margen confirmado.** Propongo que sí sirva de estimación etiquetada.

## Diagnóstico (corrido en producción el 2026-10-10)

- En ASTERIA el margen 0 **sí** viene del «Stock inicial» leído como compra: $7.538.005 de mercadería en la ventana, todo catálogo importado como `purchase` + `catalog_initial_stock`. M·1 no lo resuelve: lo resuelven la reparación **M·R** y el health score con la definición de M·2 (los dos agregados al plan v3).
- Hasta la reparación, M·1a clasifica esos movimientos como origen `compra`, sin contrato confirmado. Después de M·R pasan a `stock_inicial`, que es lo que son.
- 375 de 380 productos con costo lo tienen sólo del catálogo: la decisión 3 define que en ASTERIA se vea un margen **estimado y rotulado**.

## Riesgos

- **Cascada** ([[feedback_correccion_en_cascada]]): M·1c toca 10 rutas de escritura. Si la revisión encuentra problemas creados por M·1c, se achica la decisión en vez de apilar mecanismo.
- **Movimiento y gasto sin FK:** anular uno sin el otro ya puede pasar hoy. M·1 lee sólo el movimiento y no lo arregla; queda registrado para B·5/D·7.
- **Deploy en paralelo** api/worker: las columnas son nullable y el código viejo las ignora.
