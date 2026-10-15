# M·P — costo de reposición como principal, costo promedio como dato (antes de M·3)

> Plan aprobado por el dueño el 2026-10-13. Reemplaza al borrador del mismo día que ponía el promedio como costo principal.

## Contexto
Con inflación alta, el promedio ponderado (PPP) arrastra compras viejas de nominal bajo y **subestima lo que cuesta reponer**: con 5% de inflación mensual y 12 meses de stock, queda unos 22% por debajo de la última compra. Si un negocio fija precios con ese costo, se descapitaliza.

**Decisión del dueño (2026-10-13): la reposición es el número principal; el promedio es un dato.**
- **Principal, sin cambios:** el costo de cada venta sigue siendo el de **última compra** a la fecha de la venta (`costo_de_referencia` + `costeo_de_ventas`). `unit_cost_ars` sigue siendo el costo vigente de M·1c. El score, las alertas, `FactsService.margen_bruto` y el punto de equilibrio no cambian.
- **Dato nuevo:** el costo promedio ponderado móvil por venta y por producto, más dos lecturas:
  - el **«margen estimado con costo promedio»**;
  - la **«brecha entre costo promedio y reposición»**. No se llama «ganancia por inflación»: el cálculo muestra una diferencia de costo, no prueba que la cause la inflación, y puede ser negativa.
- **Quedan sin efecto** las decisiones del mismo día que suponían el PPP como principal: recostear sólo lo estimado, el costo tipeado sólo sin compras y el catálogo con el promedio. Lo tipeado y el catálogo siguen con las reglas de M·1c.
- Pensado para **todos los negocios**: POS con mucho volumen, sólo planillas, rubros por peso, cuentas nuevas y viejas.

La revisión externa del 2026-10-13 (7 puntos más 2 de ejecución) está incorporada abajo.

## M·P1 — las reglas del promedio (dominio puro)
Archivo: `backend/app/domain/costo_promedio.py`. Sin sesión ni ORM.

**Fórmula:** en cada compra con costo conocido, `prom = (u_conocidas × prom + qty × costo) / (u_conocidas + qty)`.

**Ejemplo de aceptación:** compro 10 a $100, vendo 5 y compro 10 a $200, y el promedio da **$166,67**. Si se anula esa venta, da **$150**: el test tiene que verlo.

**Estado por producto:** `u_conocidas`, `u_sin_costo`, `prom`, `estado` y `motivo`.
- **Orden determinista:** fecha de negocio (`COALESCE(occurred_at, created_at)`), luego la clase del evento (apertura → compra → ajuste positivo → venta/merma/ajuste negativo), luego `created_at` y por último `id`. La lectura «costo a la fecha F» devuelve el estado después del último evento con fecha ≤ F, en ese orden.
- **Compra sin costo (punto 2):** suma a `u_sin_costo`, no a `u_conocidas`. El promedio sigue siendo el de las unidades con costo, pero el estado pasa a **`incompleto`** e informa la fracción sin costo.
  - Las salidas consumen las dos cantidades en proporción, porque las unidades son fungibles.
  - El estado vuelve a **`medido`** sólo con un stock conocido que llega a ≤ 0 o con un ancla nueva.
  - Nunca se valoriza en silencio una unidad sin costo al promedio anterior. En el caso 10 a $100 + 10 sin costo + 10 a $200, no da $133,33: da $150 sobre 20 unidades conocidas, con «incompleto, 33% sin costo».
- **Stock previo conocido ≤ 0 (punto 3):** reiniciar el promedio con la compra es una convención documentada (motivo «reinicio: el stock previo estaba agotado»). **La cantidad no se perdona:** si se vendieron 5 de más, una compra de 10 deja 5 (`stock + qty`), no 10. Un costo 0 no es un costo conocido: cuenta como compra sin costo.
- **Stock no reconstruible (punto 3):** ledger complejo (`return` o ajuste sin tag) o stock fuera del ledger.
  - El estado pasa a **`sin_medicion`** y el promedio es NULL. **Una compra sola no lo recupera.**
  - Sólo lo recupera un **ancla**, es decir, un conteo con stock ABSOLUTO. Hoy el ledger no guarda ninguno: los movimientos de catálogo son deltas. El tipo `ancla` queda en el dominio para cuando exista un conteo de inventario.
- **Apertura:** `adjustment` + `catalog_initial_stock` es una capa con su costo **en su fecha de negocio**: un delta, igual que en el ledger. No va al inicio de todo, como en el chequeo temporal (punto 5).
  - Si la apertura es posterior a otros eventos, se suma como una entrada con costo; no fija el stock.
  - El catálogo que reparó M·R (`purchase` + `catalog_initial_stock` cuyo gasto anuló una corrida `MR_STOCK_INICIAL_APERTURA`) recibe el mismo trato, en su fecha.
- **Unidad:** se calcula por unidad de venta (`qty_base / factor`).
- **Costo de cada compra:** `unit_cost_final` o, si falta, `unit_cost`. El dato nunca se confirma.

**Tests:**
- el de aceptación y la anulación de la venta previa ($166,67 → $150);
- costo desconocido (incompleto, recuperación por agotamiento y por ancla);
- reinicio contra `sin_medicion` y la recuperación por ancla;
- apertura fechada y el catálogo reparado;
- orden dentro del mismo día;
- producto fraccionado;
- una compra con fecha vieja que sólo cambia lo posterior;
- la brecha según la rotación con 5% mensual.

## M·P2 — historial materializado y contrato de consistencia
**Migración** aditiva e idempotente, bajo `pg_advisory_xact_lock`, con la cadena documentada:
- **`product_cost_history`:**
  - columnas: `tenant_id`, `product_id`, `orden` (la clave determinista), `vigente_desde`, `u_conocidas`, `u_sin_costo`, `costo_promedio` (NULL si no hay medición), `estado`, **`stock_conocido`** (sin esta columna, un checkpoint posterior a un evento complejo volvería a dar el stock por conocido y una compra recuperaría la medición, cosa que la regla prohíbe), `motivo`, `movement_id` y `revision`;
  - un registro por cada **compra o ancla**: el estado justo después.
- **`product_cost_state`:** una fila por producto con `revision` y `pendiente_desde` (la fecha mínima sucia; NULL si está al día).

**Precarga específica (punto 5):** `costo_promedio_repository.py`. Trae por producto, en una sola pasada y con el orden determinista:
- los movimientos con costo, id y origen, **incluidas las compras sin costo**;
- las marcas de reparación de M·R (los gastos anulados por esa corrida);
- las ventas de `sales_entries` (las B11 pendientes no entran).

Reutiliza `classify_stock_movement`, pero no `construir_linea_de_tiempo` ni `consulta_todas_las_candidatas`: no traen costos, ids ni compras sin costo.

**Disparadores (punto 1).** Un único listener ORM (`after_flush`, como `costeo_de_ventas._anotar_ventas`) sobre `SaleEntry` e `InventoryMovement` marca `pendiente_desde = min(pendiente_desde, fecha)` cuando:
- **cualquier compra o ancla** se crea, se anula, se reactiva o cambia;
- **una venta, merma o ajuste** se crea, se anula, se reactiva o cambia de cantidad, **y existe un registro del historial con fecha posterior**. Una venta de hoy sin compras después no ensucia nada: la próxima compra la cuenta al reconstruir desde el último registro;
- **una venta cambia de producto o de fecha**: se marcan los dos productos, desde la fecha menor;
- **una venta B11 resuelve su salida pendiente** (`pos_discrepancy_service`).

Además:
- La deduplicación marca al producto canónico desde la fecha mínima de los movimientos que re-apunta.
- **Escrituras Core que esquivan el ORM** (scripts, `update()` masivos, relectura): se listan en el PR y cada una llama a `marcar_pendiente` explícitamente. Un script de verificación compara el historial con una reconstrucción completa, porque Beat no está desplegado y no hay barrido nocturno.

**Reconstrucción (punto 6):**
1. **Checkpoint:** el último registro con `orden` < `pendiente_desde`. Desde ahí se re-proyectan los eventos.
2. **Cálculo fuera de cualquier lock**, en una sesión de lectura.
3. **Publicación atómica:** una transacción corta toma el advisory lock **por producto** (no el del tenant). Si `revision` cambió desde que empezó el cálculo, descarta y reencola: así un job atrasado nunca pisa uno más nuevo. Si no cambió, reemplaza los registros desde el checkpoint, incrementa `revision` y limpia `pendiente_desde`.
4. **Job Celery** idempotente por `(tenant, producto)`, con debounce. Reencolar es no-op si ya hay uno pendiente.

**El POS nunca espera al historial:**
- No toma el lock por producto.
- Lee el último registro.
- Si la fecha de la venta es ≥ `pendiente_desde`, el dato queda **NULL con el motivo «recalculando»** y el job lo completa al publicar.

**Benchmark en Postgres**, con presupuestos fijados antes de escribir código. Si no se cumplen, no se mergea:

| Medición | Presupuesto |
|---|---|
| Reconstrucción completa de un negocio (10.000 productos, 200.000 movimientos) | ≤ 120 s |
| Reconstrucción de un producto con 50.000 eventos | ≤ 2 s |
| Latencia agregada al confirmar una venta del POS | p95 ≤ 10 ms, p99 ≤ 25 ms |
| Espera del POS por locks con una reconstrucción concurrente | p99 ≤ 50 ms |

Incluye ventas concurrentes y un producto con mucho historial.

**Pruebas concurrentes `-m postgres`:**
- una compra mientras corre la reconstrucción, con la revisión que descarta al atrasado;
- POS vendiendo durante la publicación.

**Script `backfill_costo_promedio.py`:** sólo del historial, porque la columna de las ventas recién existe en M·P3. Dry-run, `--apply` por negocio o por lotes, reanudable. Incluye el informe de sombra por rubro: brecha, % `incompleto` y % `sin_medicion`.

## M·P3 — el dato en cada venta
- **Migración:** `sales_entries.cost_avg_unit_ars`, `cost_avg_ars` y `cost_avg_estado`. NULL quiere decir sin medición.
- `costeo_de_ventas._costear_del_tenant` los escribe junto al snapshot principal, leyendo el historial. **`cost_*` no se toca.**
- La publicación de una reconstrucción recalcula el dato de las ventas posteriores a la fecha afectada. Es derivado, no congelado.
- **Backfill de ventas** (se mueve acá, ejecución): se extiende `backfill_costo_promedio.py`.

## M·P4 — mostrarlo
**FactsService (punto 4).** El margen principal queda intacto. Lo nuevo se calcula sobre la **intersección**: ventas que tienen el costo de reposición y el promedio (`medido`), con el mismo tratamiento fiscal (`base_comparable` por régimen) y el **mismo denominador**:
- `margen_reposicion_comun`, `margen_promedio_comun` y la **brecha** en puntos (puede ser negativa);
- la **cobertura conjunta** (importe de la intersección sobre el total vendido). Por debajo del umbral del 50%, el dato queda ausente (no-invention).

**Textos:**
- El chat (`margen_texto`) y la explicación del margen usan «margen estimado con costo promedio» y «brecha entre costo promedio y reposición», siempre con la cobertura.
- En la pantalla de productos: «Costo de reposición» (`unit_cost_ars`) y «Costo promedio (estimado)», con el estado si es `incompleto` o `sin_medicion`.

**La brecha es efecto inflación, no otro margen (agregado 2026-10-14).** Con inflación alta, el promedio arrastra compras viejas y la diferencia con la reposición es **ganancia por tenencia**: lo que el negocio «ganó» por tener el stock mientras subían los precios, y que vuelve a pagar al reponer.
- En el chat y en pantalla, la brecha se rotula «efecto inflación sobre el stock (ganancia por tenencia)», en pesos y en puntos, siempre con la cobertura conjunta. Nunca se presenta como un segundo margen ni se suma al margen principal.
- El margen principal sigue siendo el de reposición. No se indexa ningún costo por IPC: si más adelante sirve, va como dato aparte y rotulado («costo estimado ajustado por inflación»), nunca como margen.

Todo va detrás de una habilitación para mostrarlo (patrón `config/*_rollout.py`): primero la sombra y el informe al dueño. El score de salud **no cambia**.

## Lo que no resuelve
- **Cobertura:** las ventas sin producto siguen sin costo (M·3 / B·5).
- **Reposición vieja:** si un producto no se compra hace meses, la última compra también se atrasa. Indexarla es un paso aparte.
- **Moneda:** las compras en dólares quedan afuera.

## Archivos
- **Nuevos:**
  - `domain/costo_promedio.py`
  - `persistence/repositories/costo_promedio_repository.py`
  - `application/services/costo_promedio_service.py`
  - el job en `jobs/`
  - los modelos y las migraciones
  - `scripts/backfill_costo_promedio.py`
- **Tocados:**
  - `persistence/costeo_de_ventas.py`
  - `product_dedup_service.py`
  - `pos_discrepancy_service.py`
  - `facts_service.py` + `facts_provider.py`
  - `agents/shared/margen_texto.py`
  - `frontend/src/app/(protected)/products/page.tsx`
- **Reutilizados:**
  - `classify_stock_movement`
  - `maintenance_lock_service`, con un advisory lock por producto nuevo al lado del del tenant
  - `domain/sale_unit.importe`
  - `base_comparable`
  - la mecánica de scripts de M·2c

## Verificación
- **Por PR:** suite completa con el env de CI (`-n 6`), `ruff check .`, `mypy app`, `pytest -m postgres -n 0` contra `vektor_pgtest`, revisión adversarial, CI verde y merge.
- **M·P2:** benchmark dentro del presupuesto y las pruebas concurrentes.
- **M·P4:** `tsc`, `lint` y `jest`.
- **Ensayo local** en un tenant demo con compras de precio creciente:
  - el margen de reposición no cambia;
  - el margen con promedio sobre la intersección es mayor;
  - la brecha es la diferencia;
  - anular una venta vieja recalcula.
- Antes de mostrárselo a clientes, el dueño revisa el informe de sombra.

## Cómo quedó
### M·P1 (dominio puro): `backend/app/domain/costo_promedio.py`
- `clasificar(movement_type, source_type, qty, catalogo_reparado=)` → `apertura | compra | entrada | salida | complejo | None`.
  - Un `sale` del ledger con `source_event_id` `sale:{id}` (venta en vivo, POS o el replay histórico, que también escribe `sale`) o el legado `manual_batch:` se ignora: la venta ya está en `sales_entries`. Cualquier otro `sale`, como el «saqué N» del chat (UPDATE_STOCK, sin `SaleEntry`), es una salida.
- `proyectar(eventos, desde=, stock_fuera_del_ledger=)` devuelve cada evento con el estado que deja.
- `registros()` da lo que se materializa: compras, aperturas y anclas.
- `costo_a(proyeccion, corte)` da el estado al cierre del día.
- **Orden:** día de negocio → clase → hora → carga → id. La clase va antes que la hora, para que dentro del día los créditos vayan antes que los débitos aunque la venta sea de la mañana.
- **Revisión adversarial:** cinco correcciones.
  - El consumo proporcional queda exacto: antes, un residuo de 1E-28 impedía volver a «medido».
  - El faltante de stock no se perdona al reiniciar.
  - El costo 0 cuenta como sin costo.
  - Las salidas del chat que no tienen venta registrada cuentan como salida.
  - Las fechas con y sin zona se normalizan antes de ordenar.

  Para M·P2 quedan dos pendientes. El historial guarda `stock_conocido`. Y un movimiento sin `occurred_at` cae en `created_at`, que es UTC real: de 21 a 24 hora AR cae en el día siguiente, así que la precarga tiene que normalizarlo.
- **El ledger hoy no tiene ningún ancla con stock absoluto:** los movimientos de catálogo son deltas, igual que la apertura. Por eso un producto que cae en `sin_medicion` (devolución, ajuste sin procedencia o stock fuera del ledger) no se recupera hasta que exista un conteo absoluto. El tipo `ancla` ya está en el dominio para ese momento.
- Tests en `app/tests/domain/test_costo_promedio.py`:
  - aceptación $166,67 y la anulación de la venta ($150);
  - costo desconocido ($150 sobre 20 conocidas, no $133,33);
  - reinicio contra `sin_medicion`;
  - apertura fechada y catálogo reparado;
  - orden dentro del día;
  - producto fraccionado;
  - compra con fecha vieja;
  - checkpoint;
  - brecha con 5% de inflación mensual: menos de 3% con medio mes de stock y más de 15% con 12 meses.

### M·P2a (historial materializado, modo sombra)
**Decisiones del dueño (2026-10-13):**
- **El stock sale del ledger** (`inventory_movements`), no de `sales_entries`, para que coincida con el stock del sistema. Una planilla de ventas importada sin descontar stock haría que el stock reconstruido quedara negativo y el promedio se reiniciara a cada rato.
  - En el dominio, todo `sale` pasa a ser una salida.
  - Un producto con ventas vivas sin un movimiento que las descuente queda `aproximado`. Las B11 pendientes no cuentan.
  - Esto reemplaza la regla de M·P1 que ignoraba los `sale:` porque las ventas se tomaban de `sales_entries`.
- **M·P2 se parte en dos.** M·P2a: tablas, reconstrucción completa, backfill e informe de sombra. M·P2b: disparadores, job, reconstrucción desde una fecha, concurrencia y benchmark.

**Qué quedó:**
- Migración `20261013_0001`: `product_cost_history` (con `stock_conocido` y `aproximado`) y `product_cost_state`.
- `costo_promedio_repository.cargar_historias`.
- `costo_promedio_service.reconstruir` / `costo_a`.
- `scripts/backfill_costo_promedio.py` con el informe de sombra.
- **Revisión adversarial:** sin P1. Correcciones:
  - **Un clamp anterior** (vender de más y después comprar) ya no manda el producto entero a «stock fuera del ledger». Se simula el stock del sistema con el clamp en orden de carga; si cierra así, el producto queda `aproximado`.
  - **Se materializa todo evento que cambie lo que se lee:** una devolución o un ajuste sin procedencia después de la última compra, o una salida que vuelve de `incompleto` a `medido`. El CHECK de `clase` admite las seis clases.
  - **Menores:** las ventas con cantidad ≤ 0 no marcan `aproximado`, y la brecha excluye los productos aproximados.
- **Límites conocidos:**
  - Las filas del ledger anteriores a M·2a sin `occurred_at` se fechan con `created_at` llevado a hora AR, así que una carga de 21 a 24 hora AR puede quedar en el día siguiente.
  - El backfill commitea por negocio, no por lotes reanudables dentro de un negocio.
  - Dos backfills a la vez pueden chocar en el `INSERT` de `product_cost_state`: da error, no corrompe nada. Esto se resuelve en M·P2b.
- **Decisión pendiente del dueño (antes de M·P3):** `POST /products` y el `PATCH` de stock fijan `stock_units` sin escribir un movimiento. Esos productos quedan con stock fuera del ledger y en `sin_medicion` para siempre: probablemente la mayor fuente de `sin_medicion` en negocios reales. Hay dos opciones:
  - tratar la diferencia como unidades sin costo al inicio, con lo que el producto queda `incompleto`;
  - que el alta y la edición de stock escriban un ancla.

  Se decide con el informe de sombra a la vista. **Decidido el 2026-10-14 y resuelto en M·P2c (ver abajo).**
- **Ensayo sobre una copia de la base local de desarrollo:** los negocios cargados por seed salen con todo su stock fuera del ledger y quedan `sin_medicion`, como corresponde. Uno con compras y ventas reales da 45 productos medidos, con brecha mediana 0% y p90 3%.

### M·P2b (el historial al día solo)
El plan v2, con la revisión externa del 2026-10-13, cambió tres cosas respecto de lo escrito arriba.

**No hay checkpoint.** Se reconstruye el producto entero y se publica:
- **parcial:** se borran y escriben sólo los registros con día ≥ `pendiente_desde`, cuando el producto ya estaba publicado y la marca no es global;
- **completa:** con `pendiente_desde = date.min` o sin publicación previa.

Un checkpoint obligaba a guardar el estado intermedio completo (stock conocido, unidades sin costo, marcas globales) y a confiar en él. La proyección entera de 50.000 eventos tarda ~0,5 s, así que el ahorro no paga ese riesgo.

**La omisión es la regla, no los disparadores.** La venta normal del POS es lo único que no marca: queda al final de la línea de tiempo y no cambia nada publicado. Todo lo demás marca, y lo que mueve una marca global (`aproximado`, `stock_fuera_del_ledger`) marca todo el historial. El detalle está en `docs/claude/plataforma.md`.

**El POS sí toca el estado cuando marca.** Hace un upsert de una fila, sin advisory lock. La publicación toma `FOR UPDATE` sobre esa fila recién al final, así que la espera dura lo que dura cerrar la publicación y no el cálculo.

**Topes ajustados por el dueño (2026-10-13).** Dos mediciones quedaron fuera del presupuesto original, con la causa medida:
- **Venta del POS que marca: p95 ≤ 15 ms** (era 10; la omitida sigue en ≤ 10). Marcar son dos sentencias: el `SELECT` que decide y el upsert. En el Docker de la Mac cada viaje a la base ya cuesta 1,6 ms (un `SELECT 1`).
  - Se probó decidir y marcar en un solo `INSERT … SELECT … ON CONFLICT … RETURNING` y salió peor: p95 ~14 ms para las dos ventas, también para la omitida.
- **Rehacer todo el historial de un producto: ≤ 4 s** (era 2; la compra nueva sigue en ≤ 2 s). Con 50.000 eventos se reparte así: carga ~1,0 s, proyección ~0,5 s y escritura ~1,7 s.
  - De la escritura, ~1,1 s son los chequeos de las dos FK de `product_cost_history` (tenant y producto), uno por fila: 25.000 filas en una sola tanda (medido con un `INSERT` de 25.000 filas, con FK 1,47 s y sin FK 0,37 s).
  - Se descartó sacar las FK: con ellas, borrar un negocio no deja filas huérfanas.
  - Corre en el worker y no frena al POS. En esta Mac, SQLAlchemy corre además sin su extensión compilada (`has_compiled_ext() == False`), lo que encarece la carga.

**Cómo mide el benchmark.** Las mediciones 3 y 4 cronometran el tiempo DENTRO de los hooks del listener, con sus consultas incluidas. La primera versión restaba percentiles del commit con y sin listener. Con un fsync de ~25 ms de mediana, esa resta daba entre +8 y +21 ms en dos corridas idénticas: era ruido de la máquina, no costo del listener. La resta sigue en la tabla como referencia.

**Benchmark** (`scripts/bench_costo_promedio.py`, Postgres local en Docker, Mac del dueño, tamaño del plan):

Dos corridas seguidas del mismo código, el 2026-10-14 con la Mac libre (carga ~3–4, mayormente la VM de Docker):

| Medición | Corrida 1 (7:55) | Corrida 2 (8:10) | Tope |
|---|---|---|---|
| 1. Reconstrucción completa (10.000 productos, 100.000 registros) | 50,6 s | 44,6 s | ≤ 120 s |
| 2a. Recálculo de 1 producto (50.000 eventos), compra nueva | **2,29 s** ✗ | 1,50 s | ≤ 2 s |
| 2b. Ídem, todo el historial | **5,17 s** ✗ | 3,27 s | ≤ 4 s |
| 3. Venta del POS omitida (en el listener) | p95 9,9 · p99 12,2 ms | p95 7,2 · p99 10,1 ms | p95 ≤ 10 · p99 ≤ 25 |
| 3. Venta del POS que marca (en el listener) | **p95 17,3** · p99 22,1 ms ✗ | p95 13,8 · p99 19,8 ms | p95 ≤ 15 · p99 ≤ 25 |
| 4. POS con recálculos concurrentes (normal / incompleto) | p99 20,7 / 19,9 ms | p99 20,5 / 24,9 ms | p99 ≤ 50 |
| 5. Import de 5.000 ventas + POS + recálculos | 8,8 s · p99 29,7 ms, sin deadlock | 13,1 s · p99 46,3 ms, sin deadlock | sin deadlock · p99 ≤ 50 |
| Referencia: mediana del commit del POS sin listener | 28,3 ms | 23,0 ms | — |

**La variabilidad de la máquina es del orden de los márgenes.** La referencia, que no toca el código de M·P2b, varió un 23% entre las dos corridas, y el recálculo de un producto un 50%. Las corridas de la noche anterior (con un proceso ajeno al 100% de CPU) dieron 2a ~2,1 s, 2b ~5 s y la venta que marca p95 17,6–19,3 ms. El dueño decidió mergear con la corrida que entra completa (2026-10-14). Para mediciones futuras en esta Mac, el criterio razonable es la mediana de varias corridas, no una suelta.

### M·P2c (la carga manual queda igual que un dato importado)
**Decisión del dueño (2026-10-14):** «lo que se carga a mano también tiene que tener un costo y un margen, y una vez registrado no debería quedar diferente de otro dato importado por un archivo». El precio de compra es obligatorio; si incluye el envío, no.

- **Alta con stock → apertura**, el mismo movimiento que un catálogo importado en «ya la tenía» (`adjustment` + `catalog_initial_stock`, con su costo).
- **Edición del stock → ajuste manual** (`manual_adjustment`) por la diferencia, incremental. No es una apertura: ese origen lo leen el chequeo temporal y la deduplicación como saldo de apertura, y una corrección de +3 seis meses después los rompía. Una suba pregunta «¿compra o corrección?»; la compra va por «Registrar compra», que deja gasto, caja y proveedor.
- **Costo promedio:** una entrada de ajuste manual con costo declarado se promedia con ese costo; la de una reconciliación sigue sin costo.
- **El PATCH no pisa ventas:** `stock_units_esperado` → 409 si cambió.
- **Datos viejos:** `scripts/anclar_stock_manual.py` ancla la diferencia positiva en el alta, con el costo conocido a esa fecha (nunca el de hoy: inflación) o sin costo. El `--apply` en Neon lo autoriza el dueño.
- **Pendiente (fase 3):** el import escribe el delta de una relectura de catálogo como `catalog_initial_stock`; es el mismo problema del lado del import.

### M·P3 (el costo promedio en cada venta)
**Decisiones del dueño (2026-10-14):**
- **Nunca se muestra «recalculando»:** «que trabajen de fondo, pero que no muestren el recalculando, porque la gente es muy impaciente para esperar; en todo caso, cuando refresquen la página, que vean los cambios». La venta guarda el último promedio publicado y un `cost_avg_pendiente` interno.
- **Una venta `incompleto` guarda el promedio parcial** con su estado.
- **Topes pensados para una ferretería de 10.000+ productos:**
  - migración completa con 500.000 ventas: ≤ 3 min;
  - un producto típico con sus ventas: ≤ 200 ms;
  - las ventas del producto extremo (25.000): ≤ 2 s;
  - criterio: la mediana de 3 corridas.

  El primer tope que propuse (4 s por producto) se leía como «por producto» y, en una ferretería, era inviable.

**Cómo quedó:**
- Mig `20261014_0001` agrega `sales_entries.cost_avg_*`; `cost_*` no se toca.
- La regla pura está en `domain/costo_promedio_de_venta.py`.
- La venta se costea al confirmarse (listener del costeo, por lote).
- Cada publicación recalcula sus ventas **dentro de la misma transacción que el historial y antes del `FOR UPDATE` del estado**. La primera versión lo hacía después del commit de la publicación, y la revisión adversarial mostró dos P1: una marca en el medio, o un worker caído, dejaban ventas viejas para siempre.
- Sin historial publicado, la venta normal del POS ya no se omite.

**Benchmark** (mediana de 3 corridas):

Postgres local en Docker, Mac del dueño, 3 corridas seguidas el 2026-10-15 con la máquina libre:

| Medición | Corrida 1 | Corrida 2 | Corrida 3 | **Mediana** | Tope |
|---|---|---|---|---|---|
| 1. Migración: 10.000 productos, 100.000 registros, 500.000 ventas | 70,4 s | 74,6 s | 73,6 s | **73,6 s** | ≤ 180 s |
| 6. Producto típico con sus 1.000 ventas, todo el historial | 183 ms | 150 ms | 164 ms | **164 ms** | ≤ 200 ms |
| 7. Ventas del producto extremo (25.000), sin la publicación | 1,65 s | 1,60 s | 1,69 s | **1,65 s** | ≤ 2 s |
| 2a. Producto de 50.000 eventos, compra nueva | 1,32 s | 1,31 s | 1,45 s | **1,32 s** | ≤ 2 s |
| 2b. Ídem, todo el historial | 3,04 s | 2,84 s | 3,04 s | **3,04 s** | ≤ 4 s |
| 3. Venta del POS omitida (p95 / p99) | 4,8 / 6,7 ms | 5,0 / 7,8 ms | 5,3 / 7,8 ms | **5,0 / 7,8 ms** | ≤ 10 / ≤ 25 |
| 3. Venta del POS que marca (p95 / p99) | 9,3 / 12,0 ms | 9,1 / 11,5 ms | 9,6 / 12,3 ms | **9,3 / 12,0 ms** | ≤ 15 / ≤ 25 |
| 4. POS con recálculos (normal / incompleto, p99) | 19,0 / 16,0 ms | 13,1 / 14,2 ms | 15,5 / 15,8 ms | **15,5 / 15,8 ms** | ≤ 50 |
| 5. Import de 5.000 ventas + POS (p99), sin deadlock | 20,3 ms | 17,2 ms | 18,6 ms | **18,6 ms** | ≤ 50 |

La medición 5 detectó una regresión antes de mergear: 4–5 s de espera del POS. El `before_commit` de las marcas corría antes que el costeo según el orden de imports del proceso, y el upsert de `product_cost_state` quedaba tomado mientras se costeaban las 5.000 ventas del import. Se arregló con `insert=True` (ver `docs/claude/plataforma.md`): pasó de 4,4–5,7 s a 17–20 ms. El commit del propio import tarda ~12 s (antes ~8 s), porque ahora también escribe el costo promedio de esas ventas.

## Doc
- `docs/plans/mp-costo-promedio-ponderado.md` se reescribe con este plan en el commit de M·P1.
- `docs/claude/plataforma.md` en cada PR.
- Memoria del proyecto.
