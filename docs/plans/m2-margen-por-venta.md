# M·2 — margen por producto y por venta (mini-plan)

Fecha: 2026-10-11. Paso 5 de la fase 2 de `docs/plans/cierre-ingesta-capital-margen.md` (v3). Baja ese punto al código actual (`main` `dbf60ec0`) sin cambiar sus decisiones. El OK de la fase 2 (mergear con verde) cubre la ejecución, salvo las **decisiones abiertas** de abajo, que son del dueño.

## Qué hay hoy (verificado contra `main` 2026-10-11)

- **La línea de venta común es `sales_entries`.**
  - Todas las rutas crean una `SaleEntry` por línea: POS online y offline (`api/v1/pos.py:613`), `POST /sales`, `/sales/bulk`, `/sales/manual-batch`, `REGISTER_SALE` del chat (`cash_service.save_sale`), la ingesta plana y la multihoja, y Otros (reclasificar e import masivo).
  - El POS además guarda `pos_operation_lines` con `sale_entry_id`, y `SaleEntry.amount = line_total`, con los dos descuentos ya aplicados.
  - El pago mixto no duplica: los tenders viven aparte (`pos_tenders`) y FactsService sólo los lee para la liquidez.
- **No hay costo en ninguna línea de venta.**
  - FactsService calcula el costo vendido con el costo **actual** (`facts_provider.py:112-116`: `Product.unit_cost_ars × quantity / base_units_per_sale_unit`). Una compra nueva revalúa todas las ventas pasadas.
- **`quantity` no tiene la misma unidad en todas las rutas.**
  - El POS, `manual-batch`, la ingesta y Otros guardan unidades base (`a_unidades_base`).
  - `POST /sales`, `/sales/bulk` y el chat guardan la cantidad tal cual llega.
  - Hay que verificar si esas rutas aceptan productos con factor distinto de 1.
- **Registros de `sales_entries` que no son ventas de producto:** los cobros de fiado (`payment_method="inflow"`, sin producto) y el `/sales/bulk` agregado sin producto. Las dos cosas quedan sin costo y fuera del margen por diseño.
- **Fecha del movimiento de venta:** sale de `transaction_date`, hora AR rotulada UTC (`decrement_for_sale`, `stock_service.py:873`). La excepción es `UPDATE_STOCK` con cantidad negativa en el chat, que crea un movimiento `sale` con `now(UTC)` y sin venta asociada. Las mermas y los ajustes también usan `now(UTC)` (hallazgo de M·1b).
- **Hoy hay tres fórmulas de «margen» distintas.**
  1. `deterministic_finance.calcular_margen_bruto` (chat, agente de salud, punto de equilibrio): `(ventas − todos los gastos)/ventas`.
     - Es un margen neto, aunque se llama bruto.
     - No excluye los cobros de fiado ni filtra el demo.
  2. El subscore de salud (`health_engine._score_margin`, `health_score_service.py:107` y `:265`, el texto de MARGIN_LOW y `GET /health-scores/margin-history`): `(ventas − mercadería comprada en 30 días − fijos)/ventas`.
     - Usa la compra, no el costo de lo vendido. Es el −776% de ASTERIA.
     - Sin ventas devuelve 50, un default neutral.
  3. FactsService `margen_bruto` / `margen_neto`, con el costo actual por línea, o el gasto COGS como respaldo.
- **Los benchmarks del rubro son de margen NETO.** `decoracion_hogar.json` declara un neto de 12-22% y un bruto de 55-70%. Las bandas del subscore son netas.
- **Margen por producto:** `schemas/product.py:78` es un `computed_field` puro, `(precio − unit_cost_ars)/precio`. Lo usan siete funciones de análisis de los agentes y la columna de productos. El cajero no lo recibe (`schemas/pos.py:261`).
- **Régimen fiscal:** `base_comparable` necesita el valor **crudo**, para detectar el `registered` legacy. Los dos lectores de servicio que hay lo devuelven normalizado.
- **Costo a una fecha:** `costos_de_referencia(session, tenant_id, product_ids, corte)` (M·1a) ya resuelve por lote la fuente elegible a una fecha de corte, y `base_comparable` (M·1d) dice si ese costo es comparable.

## Qué entrega M·2

1. Cada venta nueva **congela su costo** al confirmarse, en el servidor, con la fuente elegible a la fecha de negocio de la venta y la base comparable del régimen.
2. Hay **una sola definición** de margen bruto (ganancia, % y cobertura por importe) que comparten FactsService, `deterministic_finance`, el chat y el subscore de salud.

La pantalla (dashboard, columna de productos, margen por operación, permisos de cajero en las vistas nuevas) es M·3.

## PRs (en serie; cada uno con revisión, CI y merge)

### M·2a — costo congelado por línea de venta
- **Migración aditiva e idempotente** sobre `sales_entries`, con todas las columnas nullable. NULL significa «sin costear», que es el estado de las ventas viejas.
  - `cost_unit_ars`: costo por unidad de venta, la base comparable.
  - `cost_ars`: costo de la línea, en centavos y con redondeo half-up.
  - `cost_confirmed` (bool).
  - `cost_origin`: `compra` | `stock_inicial` | `costo_cargado` | `sin_costo`.
  - `cost_reason`: el motivo de `base_comparable` cuando no está confirmado.
  - `cost_snapshot_version` (smallint).
- **Servicio `costo_de_venta_service.congelar_costo_de_ventas(session, tenant_id, sales)`**, por lote.
  - Agrupa por fecha de negocio, llama a `costos_de_referencia` y a `base_comparable` con el régimen crudo y escribe el snapshot.
  - Si una venta ya tiene snapshot, no lo toca. Así es idempotente y un reintento POS devuelve el mismo.
  - El helper nuevo `regimen_crudo(session, tenant_id)` sale del JWT.
- **Se engancha en cada ruta que crea ventas:** POS (incluido el offline, con la fecha de negocio de la operación), `/sales`, `bulk`, `manual-batch`, el chat, la ingesta plana y la multihoja, la relectura y Otros.
  - El navegador nunca manda costo.
  - Sin producto, o con la venta anulada, no hay snapshot.
- **Unidad.** El snapshot se calcula con la cantidad en la misma unidad que usa hoy FactsService. Si la ruta no garantiza unidades base y el producto tiene un factor distinto de 1, la línea queda `sin_costo` con su motivo, nunca con un número dudoso. Primero se verifica qué hacen `POST /sales` y el chat.
- **Fecha:** `UPDATE_STOCK` con cantidad negativa, las mermas y los ajustes pasan a `momento_de_negocio_rotulado` (deuda de M·1b).
- **Lo histórico no se toca en este PR.** Las ventas viejas quedan sin costear (NULL). Ver M·2c.
- **Tests:**
  - una compra posterior a la venta no cambia su snapshot;
  - una compra atrasada, cargada después pero con fecha anterior a la venta, no reescribe un snapshot ya congelado;
  - reintento idempotente del POS;
  - venta offline con fecha de ayer: elige la fuente de ayer;
  - venta por peso;
  - RI contra monotributo;
  - envío desconocido → `cost_confirmed=false` con su motivo;
  - otro tenant;
  - `-m postgres` por la migración.

### M·2b — una sola definición del margen bruto
- **Dominio puro `domain/margen.py`.**
  - `ganancia = ingreso − costo vendido`.
  - `margen % = ganancia / ingreso × 100`, sólo con ingreso > 0. El margen negativo es válido.
  - La **cobertura por importe** es el ingreso con costo confirmado sobre todo el ingreso, en el mismo universo: sin anuladas, sin demo, sin cobros de fiado y sin capital.
  - El resultado trae el ingreso total, el ingreso cubierto, el costo, la ganancia, el %, la cobertura y lo excluido con su motivo.
- **FactsService:** `_cogs` y `margen_bruto` leen el snapshot y ya no usan `Product.unit_cost_ars` actual. Las líneas sin snapshot o sin confirmar quedan fuera del margen confirmado y se informan como cobertura.
- **`deterministic_finance.calcular_margen_bruto`** delega en la definición única. La fórmula de hoy, `ventas − todos los gastos`, se renombra a lo que es (resultado operativo de 30 días) para el punto de equilibrio, y se excluyen los cobros de fiado y el demo.
- **Subscore de salud:** deja de restar la mercadería **comprada**. Ver la decisión abierta 1.
- **Tests de reconciliación:** FactsService, el chat y la salud dan el mismo número sobre el mismo universo. Se actualizan las líneas base de salud (`v1_formula.json`) con el motivo del cambio.

### M·2c — ventas históricas (script, fuera de Alembic)
- **Dry-run por tenant.** Costea las ventas viejas con la fuente elegible a la fecha de cada venta, **siempre** con `cost_confirmed=false` y el motivo «costeado después de la venta». Ninguna venta histórica sale como margen realizado confirmado.
- Se puede reejecutar sin duplicar, sólo llena NULL y es reversible.
- **El `--apply` en Neon lo autoriza el dueño aparte**, como D·3 y M·R.

### Margen estimado del catálogo (por producto)
- Un servicio arma el margen estimado con el precio vigente y `base_comparable` (con su motivo).
- El `computed_field` sigue puro, sin consultas ni tenant.
- La columna visible y los permisos van con M·3. En M·2b sólo se agrega el servicio, si los agentes lo necesitan para no seguir mezclando estimación con confirmado.

## Decisiones abiertas (del dueño)

1. **Qué mide el subscore de salud.** Los benchmarks del rubro son de margen neto: deco declara neto 12-22% y bruto 55-70%.
   - **(a) Recomendada.** El subscore sigue midiendo margen **neto**, pero con el costo **de lo vendido** de la definición única en lugar de la mercadería comprada: `(ventas − costo vendido − fijos)/ventas`. Las bandas no cambian y el −776% de ASTERIA desaparece sin esperar a M·R.
   - **(b)** El subscore pasa a margen **bruto** y hay que cargar bandas brutas nuevas por rubro: un dato nuevo por rubro, con fuente.
2. **Subscore de salud con poca cobertura de costo.** Con menos del 50% de lo vendido con costo confirmado (el umbral de M·3), el subscore no tiene base. La regla de no inventar prohíbe el 50 neutral y prohíbe el 0. **Recomendación:** el componente queda **ausente** y baja la confianza, con el motivo «cobertura de costo X%», igual que la tarjeta de M·3. Hay que ver en M·2b cómo reparte el motor el peso de un componente ausente. Si no lo soporta, se agrega.
   - Consecuencia: con las reglas estrictas de M·1d, hoy casi ninguna venta sale confirmada (envío desconocido), así que **el subscore de margen va a quedar ausente en casi todos los tenants** hasta que la compra pida envío e IVA (M·3 / B·5). Es coherente con la decisión del 2026-10-11, pero conviene que lo sepas.
