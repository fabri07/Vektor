# Ingesta: separar significado, pertenencia y reglas de negocio

## Contexto

La pantalla de mapeo de columnas obliga a renombrar prácticamente todas las columnas de un archivo y no explica por qué algunas son obligatorias. Detrás de esa queja de UI hay un problema de diseño: **hoy se mezclan tres decisiones distintas** que el usuario resuelve en un solo control.

1. **Qué significa cada columna** — decisión del usuario, libre.
2. **A qué entidad/sección pertenece** — decisión del usuario, libre.
3. **Qué reglas se aplican al confirmar** — decisión de Véktor, estricta *sólo* cuando una elección produciría datos inconsistentes, pérdida silenciosa o movimientos de stock imposibles.

> Véktor conserva todas tus columnas, propone una organización y sólo te pide intervenir donde una decisión afecta la integridad de tus registros.

Además de la UX, hay tres reglas de negocio que el código hoy no implementa: la jerarquía de entidades y su **orden histórico**, la semántica de precio unitario (`unitario × cantidad = monto`), y el **costo puesto en depósito** (compra con envío, descuentos e impuestos).

### Hallazgos verificados que condicionan el diseño

Salieron de leer el código, no del enunciado:

| # | Hallazgo | Evidencia |
|---|---|---|
| **V1** | El dispatch recorre las hojas **en el orden del archivo**. Un libro con Ventas antes que Productos resuelve todas las ventas contra un catálogo que todavía no existe. | `ingestion_import_service.py:4932` |
| **V2** | `_add_product` (hoja de catálogo) **no registra** el producto en los índices que usa `_resolve_product`. La ruta de compras sí. Un producto del catálogo del mismo archivo es invisible para las ventas de ese archivo. | `_register_product_transaction_indexes:1586`, llamado sólo en `:3331` y `:4450` |
| **V3** | Las ventas importadas **nunca descuentan stock ni crean movimiento**. | `stock_service.py:538-540` |
| **V4** | `inventory_movements` ya tiene `qty`, `unit_cost` y `movement_type ∈ (sale, purchase, loss, adjustment, return)`, más `source_type`, `source_row_hash` y `voided_at`. El motor de cantidades **no necesita tabla nueva**. | `models/inventory.py:88-104` |
| **V5** | `_apply_purchase_to_stock` **pisa** `product.unit_cost_ars` con el precio facturado de la última compra, sin envío. | `ingestion_import_service.py:1988-1989` |
| **V6** | El remito **ya decidió** que el envío es un `ExpenseEntry` OPEX `LOGISTICS` separado, sin distribuir. | `ingestion_import_service.py:5410-5427` |
| **V7** | `CANONICAL_FIELDS["expense"]` **no tiene** `quantity`, `unit_price`, `sku`, `barcode` ni `product_name`. Una planilla de compras no puede mapear ni cantidad ni precio unitario. | `column_mapping_service.py:40-53` |
| **V8** | `ExpenseEntry` no tiene columnas `quantity` ni `unit_price`. | `models/transaction.py:112-153` |
| **V9** | `_resolve_target_cols` es **last-wins** para custom fields y first-wins para canónicos. | `ingestion_import_service.py:2483-2489` |
| **V10** | La pasada que marca `required_missing` itera sobre `status == "unmapped"`. Con F-A ninguna columna queda unmapped → **el aviso desaparece en silencio**. | `column_mapping_service.py:625-640` |
| **V11** | `SUPPLIER_REFERENCE_CREATION_MODE` default de código es **`"legacy"`**, en `app/config/settings.py:276`. El plan **no** afirma que producción use `link_only`. | verificado |
| **V12** | Ya existe `stock_treatment` **por hoja** (`{context_id: "opening_balance"\|"purchase"}`). ⚠️ **Corregido en F-H3.1:** responde una pregunta CONTABLE sobre el stock inicial del catálogo (¿compra real con COGS+caja, o saldo de apertura?), NO "cómo afectan al inventario las filas de esta hoja". Son dos ejes y no se fusionan. | F10 + revisión F-H3 |
| **V13** | `decrement_for_sale` **NO** excluye ventas por `source_upload_id` — no existe tal guarda. La exclusión de hoy es estructural: el import nunca llama al servicio. Reusando `source_event_id="sale:{id}"`, el índice único parcial (`20260729_0001`) + el fast-path de `_live_sale_movement` vuelven no-op cualquier descuento en vivo posterior: la no-duplicación sale gratis, sin guarda nueva. | `stock_service.py:429-498` |
| **V14** | El chequeo de integridad **no mira el ledger** para las ventas: `stock_esperado = ancla + compras + ajustes + mermas − SUM(sales_entries.quantity)`, y los movimientos `sale` se ignoran (`MOVEMENT_CLASS_IGNORE_SALE → pass`). La fórmula **asume que toda venta descontó stock**: cierto bajo `historical_replay`, falso A PROPÓSITO bajo `current_snapshot`/`informational` → divergencias falsas si no se reconcilia. | `inventory_integrity_service.py:148,169` |
| **V15** | La reversa por borrado **ya cubre** los movimientos nuevos: voidea todo `InventoryMovement` con `source_upload_id == file_id`, incremental. Los `sale` del import se revierten gratis si llevan `source_upload_id`. Un requisito menos. | `file_deletion_service.py:956-965` |
| **V16** | Las **compras** del import ya suman stock **sin mirar `inventory_effect`**: `_apply_purchase_to_stock` corre siempre, y el registrador de la proyección sólo excluye `no_inventory`. El eje, en los hechos, gobierna **el lado de las ventas**. | `ingestion_import_service.py:1945-2020`, `_import_projection.py:61-62` |
| **V17** | `decrement_stock` clampea (`stock_units = max(0, …)`) pero `void_movement` revierte `movement.qty` **exacto** y no clampea. Un descuento clampeado + su reversa **infla** el stock. Hoy es inerte porque las ventas en vivo validan antes; un replay histórico lo vuelve real. | `stock_service.py:211` vs `:401` |
| **V18** | **No existe link persistido venta → hoja.** `source_row_ref` es el `sha256` del ancla, irreversible. Aplicar por hoja exige estampar el `context_id` (entra en `sales_entries.custom_fields`, sin migración). | `_source_row_ref:951` |

### Hallazgos del relevamiento de F-H6.d (2026-08-09)

Seis, y los tres peores no estaban en la lista de sospechas: aparecieron al ir a
verificar los otros. Todos leídos en el código, no inferidos.

| # | Hallazgo | Estado |
|---|---|---|
| **V22** | `build_line_costs` reparte sólo si `shared_mode == por_subtotal` **y** `shared_shipping > 0`. El único caller pasaba el primero y nunca el segundo → **elegir «repartir por subtotal» validaba, devolvía 200 y no repartía un centavo**. Y `validate_purchase_cost_decisions` lo ACEPTABA: la validación afirmaba que la decisión se podía honrar y después no se honraba. | ✅ cerrado (F-H6.d) |
| **V23** | `shipping_cost_line` no generaba `ExpenseEntry` en NINGUNO de sus dos modos. Con `al_costo` subía `unit_cost_ars` y el dinero no salía de ningún lado; con `gasto_aparte` (el default) era un no-op puro pese al nombre. El aviso decía «declarálo para que se apliquen» y al declararlo tampoco pasaba. | ✅ cerrado (F-H6.e) |
| **V24** | El camino plano **descarta toda decisión de costo que venga de la API**: llama al planificador con `ctx_id=None` → busca la clave `""`, y el endpoint arma el dict con el `context_id` real. Los tests lo enmascaraban porque invocan el servicio a mano con `context_id=""`, una forma que la API nunca produce. | ⚠️ rechazado pre-lease; arreglo real en **F-H6.f** |
| **V25** | `_cobrar_envios_de_la_hoja` es un closure anidado dentro de `_insert_multisheet_data`: **estructuralmente inalcanzable** desde el camino plano. Una tabla suelta con `shipping_cost` no cobra nada. Idem `_avisos_costo`, que nunca se vuelca a `counts` en el plano. | ⚠️ rechazado pre-lease; **F-H6.f** |
| **V28** | El predicado `_plano` del backend (`inferred_type != "mixed" and not multi_sheet`, la negación exacta del despacho real) es **más amplio** que el `!isMultiContext` del frontend (`contexts.length > 1 \|\| source_kind !== "table"`). Un archivo sin `multi_sheet`, con `inferred_type` ≠ `mixed` y 2+ `mapping_contexts` se dibuja como multi-hoja —con los tres ejes y el preview del reparto— y el confirm lo rechaza con 422. La otra dirección sí está cubierta: nada que la pantalla mande por su camino plano sorprende al backend. | ⚠️ abierto — **F-H6.f**. Las dos salidas están del lado del backend: que `_plano` use el criterio del despacho por contextos, o que el preview exponga «este archivo va por el camino plano» como dato. Duplicar el predicado en el frontend es la copia que ya costó ASTERIA. |
| **V26** | `product_details` se poblaba en 4 puntos, **todos del camino de catálogo**. Un producto tocado sólo por una hoja de compras quedaba con el costo pisado para siempre y el DELETE respondía `fully_reverted: true`. | ✅ cerrado |
| **V27** | `entity_changed_since_ledger` evaluaba el `updated_at` aunque el ledger no lo hubiera capturado, y el confirm de ingestión no lo captura (sólo la relectura pasa `stamp_product_updated_at`). `None != "2026-…"` → siempre «cambió»: **el DELETE marcaba TODOS los productos modificados como edición manual posterior**. Bug vivo en producción, no de esta rama. | ✅ cerrado |

Y cuatro del code-review del 2026-08-07, todos cerrados: el `IN (...)` sin chunking
de `_ya_descontadas` (65.535 binds de PG) · `customFieldCollisions` sin `strip`,
que hacía divergir la pantalla del confirm · `InventoryImpactPanel` sin invalidar
el cache de TanStack tras mover stock · los targets cruzados que `_resolve_target_cols`
descartaba **en silencio** (ahora se reportan; escribirlos es F-D).

**Conclusión de V1+V2:** la jerarquía no falla por falta de regla, falla por **orden y visibilidad**. Ordenar por entidad (product → expense → sale) resuelve la **identidad** —y sólo eso: no convierte a la compra del 20/03 en justificación de la venta del 10/03, porque eso se decide comparando fechas, no por el orden en que se aplicó (F-H2). El orden **cronológico** es otra cosa y sirve para otra: reproducir cuántas unidades había, que recién importa cuando las ventas mueven stock (F-H3.0).

---

## Orden de entrega

```
F-0  contrato e invariantes (sin cambio de comportamiento)          ✅ entregado
F-H1 jerarquía: la identidad existe antes de que alguien la busque  ✅ entregado
F-H2 identidad ≠ validez temporal (la evidencia se juzga por fecha) ✅ entregado
F-H3 efecto de inventario por hoja + cola cronológica (a→e)         ✅ entregado
F-H4 precio unitario × cantidad = monto                             ✅ entregado
F-M  reconocimiento de encabezados en dos capas                     ✅ entregado
F-H6 costos de compra agrupados (envío, costo final)  a·b·c·d       ✅ entregado
F-H6.e el flete de línea genera su gasto                            ✅ entregado
F-C  obligatorios explicados                                        ✅ entregado
F-T  medir el confirm antes de agregarle trabajo                     ✅ entregado
F-F  fechas mandan: todo movimiento afecta el inventario (.1→.4)     ✅ entregado
F-O  «Otros» y la relectura (.1 y .2)                                ✅ entregado
F-A  nombre original + preservación de edición                       ◐ sólo la preservación al cambiar de sección (60d400f8)
F-B  claridad visual + extracción del monolito                       ◐ TargetSelect/MappingOriginHint (2cbbd0d1) + fuera el % (4a0f2d8d)
─── reordenado el 2026-08-14 tras la prueba de ASTERIA en producción ───
Paso 0 medir (read-only) — COMPUERTA de toda limpieza y todo backfill      ✅ entregado (corrió contra Neon, refutó la hipótesis de «Otros»)
F-R  la relectura prueba su correspondencia                          ✅ entregado (06e69626, e8e385aa, 84ae9223)
F-S.0 catálogo↔transacciones en la MISMA carga                       ✅ entregado — 4 commits, ver abajo
F-ID identidad transversal en 3 capas (reemplaza F-S, absorbe media F-I) ✅ entregado — ID.0-ID.10 completos (schema+resolvedor+8 sitios de alta+backfill+bootstrap+wireo ingesta+dedup transfiere identificadores+código visible en frontend+helper de display para agentes)
F-CAT categorías: mapear, normalizar, inferir con evidencia, backfill  ✅ entregado (e9b9df3d), adelantada — corre antes del backfill de código
F-I  resto: comprobantes + wireo del resolvedor (recortada, ver F-ID.7)
F-E  simetría cliente/proveedor — ADELANTADA (era la última)
F-O.3 «Otros» dice por qué está cada fila
F-O.4 una fila en blanco no es un pendiente
F-V  lo que la pantalla ofrece tiene que existir (scroll, filtros)    ✅ entregado (e4558d21), adelantada
F-A/F-B  cerrar lo pendiente del panel de mapeo
F-N  nombre y apellido en una sola columna
F-D  ruteo cross-sección
F-H6.f el camino plano cobra el envío y honra las decisiones de costo
```

**Por qué se reordenó (2026-08-14).** El usuario probó la relectura de `ASTERIA_home_deco.xlsx` en
producción y reportó seis defectos con capturas. Tres no tenían fase: la relectura que propone anular
2.563 registros, la vinculación catálogo↔ventas de la misma carga, y las categorías. Los otros tres
(«Otros» ilegible, tabla que no se puede recorrer, filtro que ofrece categorías que nadie tiene) eran
huecos de pantalla sin dueño. **F-E se adelanta** porque sin definir qué crea maestros y qué sólo los
vincula, F-I puede terminarse y la pantalla seguir mostrando «Local» y «No identificado» — F-I sola no
hace aparecer ningún proveedor. F-A/F-B bajan: son pulido del panel, y lo de arriba es pérdida o
invisibilidad de datos.

**F-T, F-F, F-I y F-N se agregaron el 2026-08-10**, después de que el usuario probara un
archivo real de 9 hojas y 1.187 ventas (`Vektor_Test_DistribuidoraLimpieza_3meses.xlsx`).
El orden no es el que él enunció: pidió la velocidad primero, y va primera **la medición** de
lo que está lento, porque las otras tres fases le agregan trabajo al confirm y sin línea de
base no se puede distinguir «lo aceleré» de «lo empeoré tanto que se comió la mejora».
F-A y F-B quedan entre F-F y F-I porque tocan la misma pantalla que F-F acaba de simplificar:
hacerlo al revés es pintarla dos veces.

**F-M** se insertó a mitad de F-H6.c y no estaba en el plan original: su plan propio
vive en `docs/plans/header-recognition-fm.md`.

**F-H6.d sale detrás de una compuerta por tenant.** `PURCHASE_COST_ROLLOUT_TENANT_IDS`
(csv de UUIDs, default vacío = nadie) habilita el motor de costos —el reparto y los
ajustes de descuento/IVA/flete, o sea F-H6.c + F-H6.d—. Es la superficie que mueve
plata **sin que el usuario opte**: mapear una columna de descuento ya altera el costo.
F-H6.a (targets nuevos, inertes si nadie los mapea) y F-H6.b (no cobra nada sin
decisión explícita) salen globales. Ver `docs/runbooks/purchase_cost_rollout.md`.

### Alcance de migraciones — declaración explícita

**El programa F-0 → F-E era aditivo y sin migraciones**, y dejó de serlo con **F-I**. Todo lo demás sigue viajando en columnas existentes (`target_field` es `String`, `inventory_movements` ya tiene `qty`/`unit_cost`/`source_type`), en el payload del confirm o en `custom_fields`.

**La excepción es F-I y es deliberada:** un código externo (`CLI-01`, `PROV-03`) es identidad, y la identidad no puede vivir en `custom_fields` — necesita un índice único por tenant para que re-importar el mismo archivo no duplique maestros, y `custom_fields` es JSONB sin restricción de unicidad. Migración aditiva: columna `external_code` en `customers` y `suppliers` (detalle en F-I). Se declara acá para que la promesa de "sin migraciones" no se lea como vigente cuando ya no lo es.

**`products` NO recibe `external_code`** (corregido el 2026-08-14; el borrador anterior lo incluía). El producto ya tiene su columna de identidad: `products.sku`, con `sku_normalized` y **índice único parcial por tenant sobre activos** (`uq_products_tenant_sku_norm`, `models/product.py:127-136`), y ya es el tier 1 del resolvedor (`_resolve_product`: barcode → sku → nombre → tokens). Agregar una segunda columna de código sería un segundo eje de identidad para la misma entidad, sin nadie que arbitre cuál gana. Lo que el producto necesita no es una columna nueva sino **llenar la que tiene**: eso es F-S, y vincular las transacciones de la misma carga es F-S.0.

**F-H6-b queda FUERA de este programa.** Es una fase analítica posterior con migración aditiva, no una excepción interna. Los dos alcances:

| | Primera entrega (este programa) | Fase analítica posterior (F-H6-b, fuera) |
|---|---|---|
| Cantidad y precio facturado de compra | `inventory_movements.qty` / `.unit_cost` | ídem + por línea de gasto |
| Costo final vigente | `product.unit_cost_ars` | + histórico de costo unitario final |
| Envío | `ExpenseEntry` OPEX `LOGISTICS` | + flete asignado por línea |
| Descuentos / impuestos | custom fields + traza del import | columnas por línea |
| Distribución | calculada durante el import | persistida y consultable |
| Límite | no se puede consultar después cada componente por separado | contabilidad analítica completa |

---

# F-0 · Contrato de ingesta y red de seguridad

Fija las reglas antes de cambiar defaults o ruteo. **Cero cambio de comportamiento productivo.**

**Representación por columna** (dataclass en `column_mapping_service.py`, espejada en el schema):
`source_column` · `target_entity` · `target_field` · `custom_field_slug` · `custom_field_label` · `mapping_origin` · `user_touched`.

Separar **dato conservado** / **sugerencia de Véktor** / **decisión confirmada del usuario**. Una sugerencia automática nunca equivale a una confirmación.

**Parser único de target.** `parse_target(target) -> ParsedTarget{kind, entity, field}`, `kind ∈ (canonical, custom, cross, ignore, none)`. **Nadie más hace `startswith("custom_field:")` a mano** — barrido de los 6 sitios: `mappingRules.ts::coversRequired`, `ingestion.py::_missing_required:1291`, `column_mapping_service::save_mappings:781`, `ingestion_import_service::_resolve_target_cols:2483`, `column_risk::_is_real_target:161`, `ingestion.py:1690`.

**Cierres:**
- `_resolve_target_cols` first-wins también para custom (**V9**) + `_colliding_custom_fields()` → 422 legible. Dos cinturones: uno silencioso, uno duro.
- Una columna desconocida nunca se descarta sola. `ignore` sólo por acción explícita del usuario.
- `CROSS_ENTITY_TARGETS`: matriz explícita de rutas permitidas (la usa F-D; acá se declara y se testea).
- Test congelado: `product:stock_units` **no puede recibirse desde una venta**, aunque alguien lo agregue a la allowlist (guard por nombre, defensa en profundidad).

**Aceptación:** cero pérdida silenciosa · cero last-wins · sugerencia ≠ confirmación · suite verde sin cambios de comportamiento.

---

# F-H · Jerarquía, orden temporal, cantidades y costos

## F-H1 · Grafo de dependencias y regla de existencia

```
Catálogo / saldo inicial ─┐
                          ├─→ Producto declarado ─→ Venta inventariable
Compra de mercadería ─────┘

Cliente existente ──────────────→ Venta vinculada
Proveedor existente / sentinela ─→ Compra o gasto vinculado
```

**Regla de existencia (redacción autoritativa):**

> Una venta inventariable requiere un producto **previamente declarado** mediante catálogo, saldo inicial o compra. Si además se exige existencia física, debe haber una cantidad de apertura o una compra anterior suficiente. En importaciones históricas incompletas, **la falta de evidencia de cantidad produce una advertencia; la falta de identidad del producto rechaza la fila.**

Esto permite importar un negocio que empieza a usar Véktor con stock ya existente aunque no tenga las facturas históricas de compra. Tres estados distintos, que no deben colapsarse:

| estado | significa | efecto |
|---|---|---|
| **producto conocido** | identidad resuelta (catálogo, apertura o compra, sin importar la fecha) | la venta se importa y se vincula |
| **producto comprado** | hay apertura o compra con fecha **≤** la de la venta | se puede afirmar procedencia |
| **producto con cantidad suficiente** | el replay da saldo ≥ cantidad a esa fecha | se puede afirmar disponibilidad |

- Una venta **nunca** crea un producto.
- Una compra de mercadería **sí** puede crear un producto incompleto (`_ensure_product_for_purchase:1897` → `build_incomplete_product`).
- Una fila de catálogo puede crear un producto.
- **Venta sin columna de producto = válida**: ingreso no inventariable (servicios, honorarios, resumen diario, libro diario). No toca inventario.
- Columna de producto mapeada y **valor vacío** en una fila → venta válida sin producto.
- Producto informado y **sin identidad resoluble** → **fila rechazada a "Otros"** con `match_candidates` (reusar `_capture_unclassified:962`, que ya los acepta).

**Prerrequisito técnico (V2):** `_add_product` debe llamar a `_register_product_transaction_indexes`, igual que la ruta de compras. Sin esto, un producto del catálogo del mismo archivo es invisible para sus ventas.

## F-H2 · Identidad ≠ validez temporal  ✅ ENTREGADO

Son **dos preguntas distintas** sobre la misma fila, y colapsarlas es el error que estaba abajo del bug original:

| pregunta | qué decide | cómo se resuelve |
|---|---|---|
| **Resolución de identidad** | ¿de qué producto habla esta fila? | por **orden de pasada** sobre todo el lote |
| **Validez temporal** | ¿ese producto existía / tenía unidades en la fecha de la venta? | por **comparación de fechas**, nunca por el orden en que se aplicó |

Vocabulario, que las fases siguientes heredan:

```text
identity_resolved   = true | false
temporally_available = true | false | unknown
```

- `false` en identidad → la fila no se puede importar: va a `/otros` con `match_candidates`.
- `false` en disponibilidad → hay evidencia y es **posterior** a la venta (contador `historial_insuficiente`, nombra los productos).
- `unknown` → el archivo declara el producto **sin fecha** (un catálogo sin columna de adquisición, el caso más común) o el historial es incompleto: **no se pudo evaluar**, que no es lo mismo que "no había" (contador `historial_sin_fecha`, una línea agregada).
- Un producto **preexistente en la base** queda fuera del chequeo: tiene su propia historia y el archivo importado no es autoridad sobre ella. Sin esta regla, traer una compra reciente marcaba como injustificadas todas las ventas viejas del producto.

**Orden de pasada (identidad):** catálogos y saldos de apertura → compras → ventas. Las compras van antes que las ventas porque **una compra de mercadería declara el producto que compra**, no porque "justifique" nada. Los maestros ya vienen de `_import_master_entities`, antes del dispatch.

**Invariante:** *una compra futura nunca justifica una venta anterior.* La compra del 20/03 declara el producto **y aun así** deja la venta del 10/03 en `temporally_available = false`. Es advertencia, jamás bloqueo: un negocio que arranca con mercadería y sin las facturas viejas tiene que poder importar su historia.

Las fechas ausentes generan **ambigüedad visible** (`unknown`), nunca una precedencia inventada (invariante 2d: el timing de inserción no es evidencia).

Implementación: `_evidencia_de_producto` / `_declarar_evidencia` / `_evaluar_historial` en `ingestion_import_service.py`; avisos en `api/v1/ingestion.py`. Tests: `test_ingestion_temporal_fh2.py` (+ `test_ingestion_sheet_order_fh1.py`, que es el caso "Ventas primero, Productos después resuelve identidad").

> **La cola cronológica global se movió a F-H3 — decisión, no omisión.** Construirla acá agrega la parte más invasiva del programa sin efecto observable (las ventas importadas todavía no descuentan stock, **V3**) y con un costo real: mandar la venta del 10/03 delante de la compra del 20/03 que la declara la deja sin identidad. La cola pertenece al lugar donde el orden mueve stock, y ahí correrá sobre identidades ya resueltas.

## F-H3 · Efecto de inventario declarado por hoja

**Orden de entrega de la fase** (consecuencia de que el default sea `informational`):

```
F-H3.a  contrato: eje `inventory_effect` por hoja + defaults      ✅ entregado
F-H3.b  cálculo del impacto por fecha + warnings (NO toca stock)  ✅ entregado
F-H3.c  preview: stock inicial → movimientos → final, negativos, ambigüedades  ✅ entregado
F-H3.d  replay a un clic + fórmula de integridad reconciliada (V14) — juntos
        d.1 fórmula V14 reconciliada          ✅ entregado
        d.2 context_id estampado en la venta  ✅ entregado
        d.3 gate al confirmar (cola + /otros) ✅ entregado
        d.4 endpoint de apply                 ✅ entregado
        d.5 botón en el panel de impacto      ✅ entregado
        d.6 el replay que no se puede validar no se confirma  ⛔ REVERTIDO por F-F.1
F-H3.e  selector de efecto por hoja en la UI                      ✅ entregado
```

> **El selector (e).** `POST /ingestion/files/{id}/inventory-effects` sirve, para
> un mapeo BORRADOR, qué propone Véktor por hoja (`default_effect_for`) y entre
> qué tiene sentido elegir (`options_for`, nuevo). Las dos son reglas de dominio
> que dependen de la entidad de la hoja y de los campos mapeados —sin `quantity`
> la hoja no mueve unidades—, así que calcularlas en la UI sería la copia que ya
> divergió con el catálogo de campos. El perfil se arma con el mapeo **que manda
> el cliente**, no con el derivado: `derive_context_mapping_entries` completa las
> columnas sin mapear con sugerencias y el confirm no las usa, así que leerlas
> haría que la pantalla ofrezca un modo y el confirm resuelva otro.
>
> `options_for` acota porque `resolve_inventory_effects` valida el valor y la
> hoja, **no la combinación**: `current_snapshot` en una hoja de ventas entra sin
> 422 y lee un movimiento como si fuera un saldo. Un catálogo no ofrece "aplicar
> la historia" y una hoja que no mueve unidades no ofrece nada.
>
> **Una tabla única mandaba las columnas sin `context_id`**, y por eso su
> `inventory_effect` se rechazaba con 422 (d.6): el caso más común del producto no
> podía elegir. El confirm de ese camino ahora califica cada columna con el
> `context_id` del summary, como ya viajaban las decisiones de riesgo.
>
> Las etiquetas salen de `EFFECT_LABELS` (`domain/inventory_effect.py`), en
> castellano y **con el alcance correcto**: el eje es de la HOJA, así que dicen
> "estas filas no modificarán el inventario" y nunca "este archivo", que sería
> falso cuando el catálogo de la hoja de al lado ya dejó su saldo.

> **Forma final de d.4.** `POST /ingestion/files/{id}/inventory-replay` (gate
> `require_modify_access`, igual que `reread/apply`), body `{context_ids?, dry_run?}`.
> `preview` y `apply` son **la misma función** con un flag: si el preview corriera por
> su lado, lo que se muestra y lo que se escribe podrían separarse con el tiempo. El
> movimiento lleva `source_event_id="sale:{id}"` (idempotencia + no doble conteo con la
> venta en vivo, **V13**) y `source_upload_id` (la reversa por borrado, **V15** — probado
> de punta a punta, no asumido). Si entre el confirm y el apply el stock ya no alcanza,
> el descuento queda **pendiente** y se informa: la venta ya está en los libros y
> anularla cambiaría facturación confirmada.

> **Límite de d.3 — y por qué el archivo se rechaza en vez de importarse a medias
> (d.6).** En el archivo de **una sola tabla** no hay pasadas separadas: la misma
> fila puede dar venta, gasto y producto en la misma vuelta, así que el stock que el
> propio archivo declara todavía no existe cuando el gate mira. En el camino
> multi-hoja no pasa: el orden de pasada (catálogos → compras → ventas) garantiza
> que el stock ya esté.
>
> La primera versión **se abstenía** (`counts["replay_sin_gatear"]`) y dejaba entrar
> el archivo. Eso está mal por lo que promete el modo: elegir `historical_replay` es
> pedir que Véktor valide cada venta contra el stock, y abstenerse importaba las
> ventas sin respaldo igual, reportando el import como un replay. Hoy el confirm lo
> **rechaza con 422 antes del lease** (`replay_no_gateable`, mensaje en castellano
> con la hoja y las dos salidas: importar sin que las ventas toquen el inventario, o
> separar el saldo inicial de los movimientos en hojas distintas). No se degrada a
> `informational` en silencio, por la misma regla que ya rige en
> `resolve_inventory_effects`: un override que no se puede honrar no se ignora.
> Queda un respaldo en el importador —degrada la hoja y lo cuenta en
> `replay_degradado` con aviso— para la divergencia posible entre lo que ve el
> confirm (el mapeo declarado) y lo que ve el importador (las columnas resueltas);
> nunca aborta la operación en curso ni la reporta como un replay aplicado.
>
> **El alta de productos no era la única puerta (review #3).** El problema del
> archivo de una tabla nunca fue dar de alta un producto: es que el stock contra
> el cual habría que validar lo declara el propio archivo, en la misma pasada. Una
> **compra de mercadería** suma unidades igual que un catálogo, y el gate plano se
> calcula ANTES del bucle de filas mientras `_apply_purchase_to_stock` corre
> DENTRO — así que un libro plano de compras + ventas mandaba a "Otros" ventas que
> sus propias compras respaldan, y el mismo libro partido en dos hojas importaba
> bien. `replay_no_gateable` toma ahora `trae_compras` y rechaza también ese caso:
> es la misma situación, no una nueva.
>
> **Es transitorio, no una limitación del dominio.** Se levanta preparando el import
> en pasadas: leer todas las filas → resolver identidades y saldos de apertura
> **en memoria** (sin escribir productos) → construir compras y ventas → ordenar los
> movimientos → simular → mostrar el preview → aplicar todo atómicamente. Mientras
> eso no exista, el único camino habilitado para ese archivo es `informational`.

`c` y `d` no son "después": son **la condición** para que `d` exista. Sin preview ni fórmula reconciliada, el replay no se habilita.

> **Decisión abierta de F-H3.c — la forma del flujo.** El impacto se calcula HOY como subproducto de la inserción (necesita la identidad resuelta, que sólo existe al importar). Mostrarlo *antes* de confirmar exige elegir una de tres, y las tres cambian la UX:
>
> 1. **Dry-run real**: correr el import en una transacción y hacer rollback. Reusa todo, cuesta el doble de tiempo de import y hay que garantizar que no queden efectos afuera de la transacción (Celery, fingerprints).
> 2. **Proyección sin persistir**: recalcular identidad sin insertar. Duplica el resolvedor — exactamente lo que F-0 vino a evitar.
> 3. **Confirmar → revisar → aplicar** (recomendada): el confirm no toca stock (ya es así por el default `informational`) y devuelve el impacto; el usuario lo revisa y aplica el replay por hoja en un segundo paso. Es literalmente "el replay queda a un clic", y no necesita ni dry-run ni un segundo resolvedor.
>
> La 3 es la que sale gratis con lo ya entregado, pero convierte el replay en una acción posterior al import en vez de una opción del confirm. **Es una decisión de producto, no técnica.**
>
> **Resuelto: se eligió la 3.** El confirm no toca stock y devuelve el impacto; el replay es un paso posterior. **Regla que F-H3.d hereda:** el impacto que se muestre al aplicar se **recalcula dentro de la transacción del apply**, nunca se lee de lo que devolvió el confirm. Entre confirmar y aplicar el stock pudo cambiar, y mostrar un número viejo para una operación que va a escribir otro es exactamente lo que ya pagó F11 (por eso ahí el DELETE recalcula y su resultado es el autoritativo, no el del preview).
>
> **Superada por F-F.3 (2026-08-11): el confirm APLICA.** La opción 3 no era una preferencia de UX, era la salida a una limitación: sin cola cronológica el gate miraba un saldo estático, el archivo plano ni siquiera se podía gatear y aplicar al confirmar habría escrito descuentos que nadie podía validar. F-F.1 (créditos datados + ancla del catálogo antes de todos los eventos) y F-F.2 (saldo conocido ≠ saldo ausente) eliminaron esa limitación, y con ella la razón del segundo clic: las ventas que el gate deja entrar son exactamente las que la cronología respalda.
>
> Lo que **no** cambia y es lo que hace defendible aplicar sin preguntar: el movimiento lleva `source_upload_id`, así que borrar el archivo lo deshace (F11), y lleva `source_event_id="sale:{id}"`, así que aplicar de nuevo no descuenta de nuevo. **La regla heredada sigue en pie, y ahora rige a los dos llamadores:** el número se recalcula adentro de la transacción que escribe. Por eso el confirm no reusa el conteo del gate para avisar qué quedó pendiente — lo toma del outcome del núcleo.
>
> `POST /inventory-replay` **no** desaparece: es la vía de lo que quedó pendiente (el producto sin saldo conocido de F-F.2, o un stock que se movió entre corridas). Deja de ser el camino normal y pasa a ser el de la excepción.

> **Decisiones de F-H3.d (2026-08-06), tomadas por el usuario sobre los hallazgos V16–V18.**
>
> **1 · El stock nunca queda negativo, y la venta sin respaldo no se importa.** No se elige entre "clampear" y "dejar negativo": la fila que se quedaría sin stock **no entra como venta**, va a `/otros` con el motivo, el usuario carga el inventario que falta y la registra desde ahí con la importación masiva que ya existe. Esto **sube** el caso de `stock_historico_negativo` de *advertencia* a *bloqueante de la fila* — pero **sólo cuando la hoja declaró `historical_replay`**. Con el default `informational` nada de esto corre: el archivo se importa entero y sólo se reporta el impacto.
>
> Consecuencia de orden: decidir *cuál* venta se queda sin respaldo exige recorrer las ventas de cada producto **por fecha**, no en el orden del Excel — si no, qué fila se rechaza depende de la solapa. **Acá la cola cronológica de F-H3.0 deja de ser preparatoria y se vuelve el mecanismo.**
>
> Al **aplicar** (paso posterior) la regla no puede ser la misma: la venta ya está en los libros y anularla cambiaría facturación ya confirmada. Si entre el confirm y el apply el stock cambió y ya no alcanza, ese descuento **no se aplica y queda pendiente**, listado; el usuario carga stock y vuelve a aplicar (idempotente: sólo entran los que faltaban). Es la única salida no destructiva que respeta "las cantidades no quedan negativas".
>
> **2 · `inventory_effect` gobierna el descuento de las VENTAS, no la suma de las compras (V16).** Una compra que no sube stock está mal en cualquier modo; una venta que descuenta depende de si la historia está completa. Se documenta en el contrato y en el copy — no se cambia el comportamiento de compras, que hoy suman al confirmar.
>
> **3 · El apply es por hoja.** El import estampa el `context_id` en `custom_fields` de cada venta (**V18**, sin migración). Los archivos importados antes de d.2 no lo tienen: el apply los trata como una sola hoja **y lo dice** — un alcance silenciosamente distinto del declarado es peor que uno declarado.

### F-H3.0 · La cola cronológica (movida acá desde F-H2)

Se construye en **F-H3.b**, donde ya calcula el impacto aunque todavía no lo aplique. Dos pasadas:

**Paso 1 — Identidades, sin aplicar movimientos.** Ya entregado en F-H1/F-H2: maestros, catálogos, saldos iniciales y las identidades que declaran las compras. Al entrar a la cola, **toda fila ya sabe de qué producto habla** (`identity_resolved`); la cola nunca decide identidad.

**Paso 2 — Movimientos, ordenados por fecha.** Compras, ventas, devoluciones, mermas y ajustes de **todas** las hojas entran a una sola secuencia ordenada por:

1. fecha efectiva;
2. a igual fecha: **apertura → compra/devolución → venta/merma → ajuste**;
3. orden de hoja;
4. número de fila.

El desempate crédito-antes-que-débito ya existe en `inventory_temporal_service.replay_timeline:144` — **reusar esa función, no escribir un segundo replay.**

La cola **no reemplaza** el chequeo de fechas de F-H2: reproducir movimientos en orden dice cuántas unidades había, no si la evidencia es admisible. Una compra futura sigue sin justificar una venta anterior aunque el replay la aplique después.

> **Consecuencia estructural — la parte más invasiva del programa.** `_insert_multisheet_data` deja de ser un loop por contexto que inserta a medida que recorre. Los anclas de idempotencia siguen siendo por `(archivo, contexto, índice DENTRO DE SU HOJA)`, así que reordenar **no** los invalida — y eso ya está clavado por `test_la_huella_numera_la_fila_dentro_de_su_hoja`, que se pone rojo si el índice pasa a derivarse de la posición en la cola.

**Tests reservados para esta fase** (no se pueden escribir antes: hoy no hay movimiento de stock desde ventas que observar):

- compra anterior a la venta → venta válida (`temporally_available = true`);
- compra futura → no justifica la venta (`false`), aunque el replay la aplique;
- historial incompleto → `unknown`, distinguible de "producto desconocido";
- replay idempotente: re-confirmar el mismo archivo no aplica el movimiento dos veces;
- eliminar el import revierte exactamente sus movimientos (incremental, nunca `Σ(movimientos)`).

### F-H3.1 · Modos de inventario por hoja

Cada hoja declara qué significan sus cantidades:

| modo | comportamiento |
|---|---|
| `informational` | calcula el impacto y lo advierte, **sin modificar stock** |
| `historical_replay` | compras suman y ventas restan; **el stock final refleja las ventas** |
| `current_snapshot` | el archivo declara el saldo absoluto final |
| `no_inventory` | cantidad puramente transaccional, no asociada a producto |

**Son DOS ejes, no uno.** El plan decía que esto "extiende el `stock_treatment` que ya existe (**V12**)", con la equivalencia `historical_replay ≈ purchase` / `current_snapshot ≈ opening_balance`. **Está mal y no se implementa así.** `stock_treatment` responde una pregunta *contable* sobre el stock inicial de un catálogo — ¿es una compra real (COGS + baja de caja) o un saldo de apertura? —, que no es la pregunta de F-H3: *¿cómo afectan al inventario las filas de esta hoja?*. Fusionarlos haría que alguien que elige "las ventas de esta hoja descuentan" declare **en silencio** que su catálogo genera COGS y baja de caja. El eje nuevo (`inventory_effect`) va **al lado** de `stock_treatment`, que no cambia de semántica.

**Pregunta al usuario** cuando una hoja tiene producto y cantidad: *"¿Cómo deben afectar estas filas al inventario?"*, con default por tipo detectado:

| tipo detectado | default |
|---|---|
| Ventas históricas | **`informational`** |
| Compras históricas | **`informational`** (salvo onboarding guiado) |
| Catálogo con stock actual | `current_snapshot` |
| Saldo inicial declarado | `current_snapshot` |
| Importación operativa incremental **claramente identificada** | `historical_replay`, tras confirmación |
| Ventas sin producto / resumen contable / libro diario | `no_inventory` |

> **Decisión del usuario (2026-08-05): el default nunca es `historical_replay`.** El histórico es la capacidad correcta, pero aplicarlo automáticamente es peligroso cuando el archivo puede estar **incompleto** o **solaparse con saldos ya cargados** — y un archivo real (10.931 ventas) movería el inventario entero de una sola confirmación. Véktor calcula y muestra el impacto histórico; no toca stock hasta que el usuario elija `historical_replay` para esa hoja.
>
> **Consecuencia de orden:** el preview y la fórmula de reconciliación (**V14**) son **requisitos del replay**, no fases posteriores. El replay no se habilita sin ellos.

**Esto corrige V3**: bajo `historical_replay` las ventas importadas **sí** generan movimiento `sale` y descuentan stock. Requisitos que lo hacen seguro:

- **Preview obligatorio del stock resultante** antes de confirmar (por producto: saldo previo → movimientos → saldo final).
- **Idempotencia por archivo, hoja y fila**: el movimiento lleva `source_upload_id` + `source_row_hash` (columnas existentes, **V4**). Nunca se aplica dos veces el mismo movimiento.
- **Eliminar el import revierte exactamente sus movimientos**, vía `void_movement` con ajuste incremental. `stock_units` **NUNCA** por `setattr` ni recalculado como `Σ(movimientos)` — su reversa es exclusivamente incremental (invariante ya pagado con un incidente). Ya está resuelto por el borrado por procedencia (**V15**): alcanza con que el movimiento lleve `source_upload_id`.
- **Sin doble conteo con la venta en vivo**: reusar `source_event_id="sale:{id}"` (**V13**). No hay guarda por `source_upload_id` que "siga" excluyendo nada — nunca existió; la clave compartida hace que el índice único parcial resuelva la carrera sin código nuevo.
- **Sin doble conteo con el chequeo de integridad**: **no alcanza con el `source_type`**. La fórmula de `inventory_integrity_service` resta `SUM(sales_entries.quantity)` asumiendo que toda venta descontó (**V14**); hay que hacer que reste sólo las ventas cuyo efecto se aplicó, o los modos que no descuentan generan divergencias falsas. Es requisito del replay.
- **El stock nunca queda negativo — y tampoco se clampea (V17).** El clamp de `decrement_stock` (`max(0, …)`) descuenta menos de lo que dice el movimiento, y `void_movement` revierte el movimiento **entero**: la reversa por borrado inflaría el stock. Así que la venta sin respaldo **no se importa** (va a `/otros`) en vez de descontarse a medias. Lo que llega al descuento siempre tiene stock que lo cubre, y el clamp vuelve a ser inerte como lo es para las ventas en vivo. `InsufficientStockError` sigue sin levantarse en el replay: la fila se desvía, no explota.

**Taxonomía de incidencias, cada una con su severidad:**

| código | severidad | efecto |
|---|---|---|
| `producto_no_resuelto` | **bloqueante de la fila** | → /otros con candidatos |
| `cantidad_cero_o_negativa` | **bloqueante de la fila** | → /otros |
| `venta_sin_stock_que_la_respalde` | **bloqueante de la fila, sólo bajo `historical_replay`** | → /otros con el motivo; el usuario carga el inventario y la importa desde ahí |
| `stock_historico_negativo` | advertencia (modos que no aplican) | importa; se reporta el saldo proyectado |
| `historial_insuficiente_para_validar` | advertencia explícita | importa; se reporta que **no se pudo evaluar** |
| `cantidad_ausente` | informativa | la fila entra; no afecta stock |

Modo estricto opt-in ("bloquear si el stock reconstruido queda negativo") queda documentado como extensión, no se implementa ahora.

El resumen del import lista: filas con producto no resuelto · productos con saldo negativo · primera fecha negativa · cantidad mínima alcanzada · movimientos ignorados por falta de cantidad. Se enchufa en `ConfirmIngestionResponse.warnings` (`schemas/ingestion.py:331-338`), que ya existe y ya se pinta.

**Cantidades distinguidas:** `purchase.quantity` (suma) · `sale.quantity` (resta) · `product.stock_units` (apertura/inventario absoluto, ni compra ni venta) · ajustes (correcciones, aparte) · bonificaciones, devoluciones y mermas como movimientos explícitos, **nunca cantidades negativas ambiguas**. Todos ya existen como `movement_type` (**V4**).

Una cantidad afecta stock sólo si: producto resuelto **y** tipo de movimiento conocido **y** cantidad válida **y** el modo de la hoja lo habilita.

## F-H4 · Precio unitario y monto

Función **pura y testeable**, sin dependencias de sesión:

```
si unit_price y quantity:
    calculated = unit_price * quantity
    si falta amount:            amount = calculated
    si amount difiere > tol:    amount = calculated ; registrar discrepancia
si falta unit_price:            NUNCA derivarlo de amount / quantity
```

Tolerancia monetaria **explícita**: 1 centavo tras el redondeo a `Decimal("0.01")`.

| Datos | Resultado |
|---|---|
| unitario + cantidad | calcula monto |
| unitario + cantidad + monto coincidente | importa normal |
| unitario + cantidad + monto distinto | usa el cálculo, reporta discrepancia, **conserva el monto original** |
| sólo monto | importa monto; `unit_price` queda `NULL` |
| monto + cantidad | **no** deriva unitario |
| sólo unitario | no inventa cantidad ni monto |
| cantidad sin producto | dato transaccional; no toca stock |

**No contradice F10**, que prohíbe inferir el unitario desde un total ambiguo (en una fila histórica no se sabe si el monto es unitario o total). La derivación inversa es segura porque el usuario **mapeó explícitamente** unitario y cantidad. El comentario de `models/transaction.py:57-62` se amplía con las dos direcciones.

Evidencia preservada sin migración: el monto original discrepante va a la traza (`pipeline_events`, `STAGE_CONFIRM`) y a `custom_fields`.

### Entregado (3 commits, sin migraciones)

`domain/line_amount.py` (puro) → cableado en los **dos** caminos de inserción de ventas → **requerido condicional**, que es lo que lo vuelve alcanzable desde la pantalla.

> **El requerido tenía que moverse en la misma fase, no en F-C.** `REQUIRED_FIELDS["sale"]` incluye `amount`, así que el confirm rechazaba con 422 un archivo de precio + cantidad sin total: el cálculo habría quedado escrito y sin forma de dispararse desde la UI — el agujero exacto de F-H3.e. La regla es `amount OR (unit_price AND quantity)`, declarativa en `REQUIRED_ALTERNATIVES` (`column_mapping_service`), servida por `GET /ingestion/field-catalog` y consumida por `missingRequiredFields`: el frontend **no** tiene copia. Lo que F-C agrega encima es el *motivo* legible, no el *si*.

**Tres hallazgos verificados antes de escribir** (V19–V21 del listado de arriba): el 422 del requerido · la compuerta `wants_ventas` del camino plano, que exige columna de monto y saltea la hoja ENTERA antes de mirar una fila · el **piso en 1** de `_venta_cantidad`, que existe para el gate de replay y que, usado para derivar, le pondría `precio × 1` a cada fila con la celda de cantidad vacía. Por eso la derivación lee la cantidad **cruda** y sólo por mapeo explícito.

**Validación final por fila:** la que no tiene monto ni pareja que lo calcule va a "Otros" con el motivo, en vez de desaparecer. Consecuencia elegida, no colateral: la captura es output persistido, así que **registra huella** y el archivo corregido no la re-importa (se completa desde "Otros"). Eso reemplaza al contrato B1 —"no quemar la fila así el archivo corregido la importa"—, que existía porque hasta acá la fila no quedaba en ningún lado. Es el mismo criterio que ya rige para una fecha ilegible (F6-A2).

**Dos límites declarados.** (1) En el camino plano la captura no corre si el archivo también trae productos: ahí el bucle de productos recorre las MISMAS filas más abajo, y capturar una fila de catálogo la mandaría a la bandeja *y* haría que el bucle la saltee (`_captured_to_otros_rows`), dejando el catálogo entero en "Otros" sin crear un producto. (2) **Gastos y compras quedan afuera hasta F-H6**: no tienen `unit_price` ni `quantity` en su catálogo de campos, y derivar desde columnas autodetectadas es justo lo que F10 prohíbe.

**Armonía con F8:** `validate_column_risk_decisions` también conoce la alternativa. Sin eso, el confirm aceptaba la hoja sin monto mapeado pero *eliminar* la columna de monto —una columna casi toda vacía al lado de precio y cantidad completos, que es el caso que dispara el protocolo de riesgo— daba 422: dos validaciones diciendo cosas distintas sobre el mismo archivo.

## F-H5 · Confirmación atómica

**Antes de escribir:** resolver identidades → construir la cola cronológica de movimientos en memoria → validar relaciones bloqueantes → calcular importables / rechazadas / warnings → **preview final** (incluye el stock resultante de F-H3).

**Al confirmar:** insertar sólo filas válidas · trazabilidad de las rechazadas · buffers por identidad (no una query por fila) · idempotencia por `(archivo, contexto, fila)` intacta.

`_add_sale`/`_add_expense` pasan de `bool` a `RowOutcome(inserted, product_id, customer_id, supplier_id)`. **Riesgo alto:** `_did_insert` alimenta `_register_import_row_fingerprint`. Test de idempotencia **antes** de tocar la firma.

**Re-scope tras F-F.3 (2026-08-11).** Tres de las cinco piezas de arriba ya están entregadas por otra fase, y conviene anotarlo antes de estimar lo que queda:

| Pieza | Estado |
|---|---|
| Cola cronológica de movimientos en memoria | ✅ F-F.1 (`rows_without_stock_backing` con `CreditEvent` datados) |
| Filas rechazadas por falta de respaldo, con trazabilidad | ✅ F-H3.d.3 + F-F.2 (van a «Otros» con motivo; el pendiente se avisa) |
| El stock resultante de F-H3 | ✅ F-F.3 — ya no es un preview a mostrar antes: el confirm lo **aplica** y lo informa |

**Lo que sigue siendo F-H5, y es lo caro:** el `RowOutcome` (que es un refactor de firma con riesgo sobre las huellas de idempotencia) y el **preview final previo a escribir**. Y ese preview hay que re-justificarlo: nació para que el usuario viera el impacto de inventario antes de confirmar, y ese impacto ahora se aplica y se revierte borrando el archivo. Si lo único que queda del preview es "cuántas filas van a rebotar", eso ya se responde después, en «Otros», con la fila a la vista.

## F-H6 · Costos de compra agrupados

### Entregado: a (campos) + b (el envío se cobra una vez)

**a — `4c8900cd`.** `CANONICAL_FIELDS["expense"]` no tenía `quantity`,
`unit_price`, `sku`, `barcode` ni `product_name`. Y la heurística conoce
`costo_unitario`, `precio_costo` y `precio_compra` pero **no `precio_unitario`**,
que es como titula la columna media planilla de compras: ahí el costo se perdía
entero (producto sin costo, margen en cero, stock valuado en nada). Hay un test
que lo mide. Con los targets propios **las compras entran a F-H4**: una línea con
precio y cantidad no necesita que le escriban el total, y `REQUIRED_ALTERNATIVES`
cubre `expense` igual que `sale`.

**b — `790a7ca1` + `cca429e1`.** `domain/purchase_shipping.py` agrupa por
comprobante (proveedor + número): una cifra por comprobante se cobra UNA vez.
Cifras distintas en el mismo comprobante se cobran todas y se avisan — pueden ser
flete y seguro. El gasto es OPEX `LOGISTICS` sin producto ni stock, igual que el
remito manual, para que el mismo hecho no quede clasificado de dos formas según
por dónde entró. La huella de idempotencia es del CARGO (comprobante + cifra), no
de una fila del grupo.

> **Sin comprobante hay dos caminos, y los elige el usuario por hoja**
> (`ShippingDecision`, mismo protocolo que las decisiones de riesgo, validado
> antes del lease). «Es un solo envío» → una cifra, un cargo (sumar la columna
> convertiría un prorrateo en un total inflado; exigir una sola cifra dejaría sin
> salida a dos fletes reales). «Cada fila es un envío» → diez filas de 2.000 son
> $20.000, y la pantalla lo dice con el número antes de elegir. **No hay default**:
> sin decisión no se cobra, y el aviso ofrece además mapear el comprobante para
> que Véktor agrupe solo. El control aparece únicamente cuando la hoja mapea envío
> y NO mapea comprobante.
>
> La huella de un cargo sin comprobante incluye su fila: si no, los diez cargos
> iguales de «cada fila es un envío» colapsarían en uno y el segundo import
> cobraría de nuevo.

**Pendiente de F-H6:** la distribución (c) y el preview del grupo (d), con la
corrección de **V5** — hoy `_apply_purchase_to_stock:2057` pisa `unit_cost_ars`
con el precio de la última compra importada, aunque la columna fuera precio de
lista o facturado.

### Identidad del comprobante — sin esto no se puede distribuir nada

Una columna `Envío = 2.000` repetida en diez filas se convertiría en $20.000. **Véktor sólo distribuye un costo compartido cuando puede identificar inequívocamente el conjunto de líneas al que pertenece. Si no puede agruparlo, el envío queda como gasto separado.**

- **Targets nuevos de identidad de operación:** `purchase_id`, `invoice_number`, `order_number`, `document_number`.
- **Agrupación:** proveedor + número de comprobante.
- **Fallback por hoja/bloque** sólo con confirmación explícita del usuario, nunca automático.
- **Detección de costo repetido:** misma cifra de envío en N filas del mismo grupo → se cuenta **una sola vez** y se avisa.
- **Diferenciar tres semánticas distintas**, cada una con su target: envío **total de la factura** · envío **asignado a la línea** · envío **unitario**.
- **Preview del grupo antes de distribuir**: qué líneas lo componen, qué monto se reparte y cómo.

### Targets nuevos de `expense` (V7)

Hoy una planilla de compras **no puede mapear cantidad ni precio unitario** — ésa es la causa de que el costo entre mal. Se agregan al catálogo: `quantity`, `unit_price`, `sku`, `barcode`, `product_name`, `shipping_cost`, `discount`, `taxes`, más los cuatro de identidad de comprobante. Los montos y cantidades entran en `SINGLE_VALUE_FIELDS["expense"]`.

**Los precios dejan de ser un selector genérico "Precio":**

| campo | significado | dónde vive |
|---|---|---|
| Precio unitario de compra | precio de cada unidad **en esa compra** | `inventory_movements.unit_cost` (**V4**) |
| Costo unitario vigente | costo de referencia actual | `products.unit_cost_ars` |
| Precio de lista (sugerido) | recomendado por proveedor | `products.list_price_ars` |
| Precio de venta vigente | configurado por el negocio | `products.sale_price_ars` |
| Precio unitario vendido | precio real de esa venta | `sales_entries.unit_price` |
| Monto total | total monetario de la fila | `amount` |
| Envío / flete | costo adicional de la operación | `ExpenseEntry` OPEX `LOGISTICS` (**V6**) |
| Descuento / impuestos | componentes de la operación | custom fields + traza |
| **Costo unitario final** | costo efectivo con adicionales | **calculado** |

```
costo total de la compra = subtotal − descuentos + envío + seguros + impuestos no recuperables + otros atribuibles
costo unitario final     = costo total atribuible al producto / cantidad recibida
```

### Distribución

El usuario elige; Véktor no reparte solo: no distribuir (**default**) · por cantidad · **por subtotal monetario (recomendado)** · por peso/volumen si el archivo lo trae · manual. La suma distribuida siempre cuadra con el costo original, incluido el redondeo (el resto va a la línea de mayor subtotal, determinístico).

**Default = "no distribuir"** porque es lo que el remito ya eligió (**V6**); cambiarlo en silencio alteraría los costos de todos los imports existentes. El selector aparece sólo cuando la hoja mapea un costo compartido **y** el grupo es identificable.

### Doble conteo — comprobable, no declarativo

- Envío incorporado al costo de inventario → **no vuelve a computarse íntegro** como costo de mercadería al vender. El `ExpenseEntry` del envío se marca `custom_fields.attributed_to_inventory = true` para que los agregados no lo cuenten dos veces.
- Envío no distribuido → queda OPEX `LOGISTICS` separado y **no modifica** `unit_cost_ars`.
- **Corregir V5:** `_apply_purchase_to_stock:1988` hoy pisa `unit_cost_ars` con el precio facturado de la última compra. Pasa a escribir el **costo unitario final** cuando hay distribución, y a no pisar cuando el usuario declaró que esa columna es precio de lista o facturado. El precio facturado sigue por movimiento en `inventory_movements.unit_cost`: **los dos valores se conservan.**

### Flete implícito: el caso que no necesita código y desnuda el default

Un archivo que **no desglosa el envío** porque el proveedor ya lo cargó en el precio unitario no rompe nada y **da el número correcto**: sin columna de envío no hay `ShippingDecision`, `build_line_costs` corre con `shipping_line = 0` y `shared_shipping = 0`, y `unit_cost_ars` queda con el flete adentro — que es el costo real de adquisición, justo lo que la distribución intenta reconstruir cuando el proveedor sí lo desglosa.

El problema es que **los dos caminos no convergen**. Misma compra, $100 de mercadería + $10 de flete:

| | `unit_cost_ars` | Gasto aparte | Margen del producto | Stock valuado |
|---|---|---|---|---|
| Flete implícito | **110** | — | menor | mayor |
| Flete desglosado + `no_distribuir` (**default**) | **100** | $10 OPEX `LOGISTICS` | mayor | menor |

Los dos cuadran en el total ($110 salieron de la caja), pero el margen por producto y la valuación de stock dan distinto. La consecuencia incómoda: **el default hace a Véktor menos preciso justo cuando el proveedor le dio más información.**

Y con **V5** vivo hay un caso peor: si el mismo producto entra una vez implícito (110) y después desglosado (100), `_apply_purchase_to_stock` pisa el costo y **baja de 110 a 100 sin que nada se haya abaratado** — cambió el formato de la planilla, nada más.

Dos piezas, las dos dentro de F-H6.c/d:

1. **Trazar si el costo incluye flete.** `purchase_cost.py` ya reserva `_vektor_costo_base`, pero hoy vive en la fila del gasto, no en el producto. Sin ese dato, comparar dos costos es comparar cosas distintas sin saberlo — mismo criterio de procedencia que ya gobierna los agregados.
2. **Cerrar V5** (arriba): que una compra nueva no pise un costo que incluía flete con uno que no lo incluye.

**Lo que NO se hace:** cambiar el default a "distribuir". Alteraría el costo de todos los imports existentes, que es exactamente lo que el default vino a evitar (**V6**).

**Archivos F-H:** `ingestion_import_service.py` (dos pasadas en `_insert_multisheet_data`, `_add_product` + índices, `_add_sale:4172`, `_add_expense:4270`, `_apply_purchase_to_stock:1944`, `RowOutcome`), `inventory_temporal_service.py` (reuso de `replay_timeline`), `inventory_movement_origin.py` (`sale_import`), `stock_service.py` (reversa incremental), `column_mapping_service.py` (targets de expense), módulo nuevo de aritmética de precios/costos, `schemas/ingestion.py` (modo de inventario por hoja + warnings estructurados).

---

# F-T · Tiempo de confirmación

**Medir antes de tocar.** El usuario reportó que el traspaso de la pantalla de mapeo a la
importación es lento, y al preguntarle dónde duele contestó: **en el confirm**. No al abrir el
mapeo, no al tocarlo.

Instrumentar `POST /files/{id}/confirm` por etapa y publicar el desglose en `pipeline_events`,
que ya es la traza del pipeline: `STAGE_CONFIRM` se emite en `api/v1/ingestion.py`, y
`_emit_confirm_failure` ya distingue las fases `lease_lost` / `import`. Etapas: lectura del
summary · validaciones pre-lease · resolución de maestros · inserción por hoja · decisiones de
costo · derivados post-commit.

**Va primera porque F-F, F-I y F-N le agregan trabajo al confirm** —replay por fechas,
resolución por código, split por fila—. Sin línea de base, la próxima vez que el usuario diga
"sigue lento" nadie va a poder decir si el trabajo nuevo se comió la mejora o si nunca hubo
mejora.

**Sospechas anotadas para atacar DESPUÉS de medir, nunca antes:** la resolución de maestros
fila por fila, y el recálculo de score que se encola en el `after_commit`
(`score_trigger_service.trigger_score_recalculation_after_commit`). Cualquiera de las dos puede
ser irrelevante; el orden "medir → optimizar" existe justamente porque la intuición sobre
dónde se va el tiempo suele estar mal.

**Aceptación:** el confirm publica su desglose por etapa · hay un número antes y otro después,
sobre el mismo archivo real (9 hojas, 1.187 ventas, 162 compras, 30 productos).

---

# F-F · Fechas mandan: todo movimiento afecta el inventario

**Lo que pidió el usuario, textual:** «eliminá todo lo que dice "no afecta el inventario"
porque todos los movimientos afectan el inventario; es vital detectar las fechas, lo que se
compró primero y lo que se vendió después, en períodos de tiempo».

`inventory_effect` baja de cuatro modos a dos:

| Modo | Cuándo es default |
|---|---|
| `historical_replay` — las compras suman y las ventas restan | ventas y compras que mueven unidades |
| `current_snapshot` — el archivo declara el saldo absoluto | catálogo con cantidad |

Desaparecen `INFORMATIONAL` y `NO_INVENTORY` de `domain/inventory_effect.py` y de sus
consumidores: `schemas/ingestion.py`, `api/v1/ingestion.py`, `ingestion_import_service.py`,
`_import_projection.py`, `inventory_integrity_service.py`, `domain/inventory_replay_gate.py`,
`jobs/recalculate_health_score.py`, `InventoryImpactPanel.tsx`.

**Una hoja que no identifica producto Y cantidad no muestra la pregunta**
(`SheetInventoryProfile.moves_units` ya sabe decidirlo). Hoy Gastos_Fijos, Clientes y
Proveedores muestran «Estas cantidades no afectan el inventario», que es el cartel que el
usuario pidió sacar. Y tiene razón en el fondo: el problema no es que la frase sea falsa, es
que esas hojas **no hablan de inventario**, así que la respuesta correcta es no preguntar.

**El ancla del catálogo sigue ignorando su fecha, y eso NO es una omisión.**
`inventory_temporal_service` lo declara: `catalog_initial_stock` es un snapshot sin fecha de
negocio, y anclarlo en la fecha del import marcaría como divergentes todas las ventas
anteriores. Se aplica como saldo de apertura **antes de todos los eventos**; la cronología
gobierna a las compras y ventas **entre sí**. Esa separación es la que evita el doble descuento
del incidente don pedro, y es exactamente la condición que faltaba para poder cambiar el
default.

**La parte grande: levantar el límite del archivo plano.** `inventory_replay_gate` hoy rechaza
pre-lease un archivo de una sola tabla que declara stock *y* ventas, porque no hay saldo contra
el cual validar: lo está cargando el propio archivo en la misma pasada. Con `historical_replay`
por default, ese rechazo dejaría de ser excepcional y rompería archivos que hoy importan bien.
El importador tiene que armar **identidades y saldos provisionales en memoria antes de
construir los movimientos** — el arreglo que el docstring del gate ya anticipaba como
definitivo, y que el camino multi-hoja hace de facto (catálogos → compras → ventas).

**Lo que se re-litiga y por qué, para que no vuelva a discutirse.** El test
`test_historical_replay_nunca_es_un_default` y el docstring de `inventory_effect.py` congelaron
el default después del incidente don pedro: un archivo de 10.931 ventas movió el inventario
entero en una confirmación y la parte ya contada en el saldo de apertura se descontó dos veces.
La regla **no se levanta por conveniencia ni porque el usuario lo pidió**: se levanta porque el
replay pasa a aplicarse por fecha y el ancla se aplica antes de todo, que era la condición que
en su momento no existía. Si alguna de esas dos piezas se cae, el default tiene que volver.

### Entregado: F-F.3 — el confirm aplica (2026-08-11)

Una **segunda pasada dentro del savepoint del confirm**, después del import, que llama al mismo
`run_inventory_replay` que el endpoint del panel. Reusar el núcleo no es prolijidad: lo que
descuenta el confirm y lo que descuenta el segundo intento tienen que ser la misma operación, o
el reintento podría descontar distinto que el primero. Sale acotada a las hojas resueltas como
`historical_replay`, con etapa propia `replay_inventario` en el desglose de F-T.

Esto **deroga la decisión de F-H3.c** (confirmar → revisar → aplicar), que no era una preferencia
de UX sino la salida a una limitación ya levantada. El detalle está en el bloque de F-H3.c.

**Los avisos pasan a ser de hechos consumados**, y el número de lo que quedó pendiente sale del
outcome recalculado adentro de la transacción que escribió, no del contador del gate: el confirm
tiene dos momentos —gatear e insertar— y publicar el número del primero para describir lo que
hizo el segundo es la misma clase de error que F11 ya pagó. En particular «No se modificó el
stock» se eliminó del aviso de proyección negativa: era el único lugar del confirm que negaba lo
que el confirm acababa de hacer.

**Medido antes de dar por buena la forma** (100 ventas, contando sentencias en el propio
confirm): la pasada cuesta **~4 sentencias SQL y 1 envío al broker por venta** —`SELECT` de
balance, `UPDATE` de balance, `INSERT` del movimiento, `UPDATE` del producto, más el advisory
lock por llamada en Postgres—. Sobre el archivo real (1.187 ventas) son ~4.700 sentencias y
1.187 mensajes a Redis **dentro del request**, que es exactamente la clase de demora que F-T
existe para no volver a introducir a ciegas. Por eso el batch va en **F-F.3.b**, adentro del
núcleo compartido y no en el llamador.

**Alcance declarado:** aplica el **confirm**. Los otros cuatro puntos que insertan ventas
—relectura (`reread_service`), los dos de chat (`pending_action_service`) y
`data_repair_service`— no descuentan. La relectura entra en **F-F.4**, donde además hay que
sellar `updated_at` (es el único camino que lo captura en el ledger, así que ahí el guard de V27
sí se prendería). Chat y reparación quedan afuera a propósito: no son imports de archivo con
efecto de inventario declarado por hoja.

### Entregado: F-F.3.b — el costo pasa a depender de los productos, no de las ventas (2026-08-12)

`stock_service.decrement_stock_bulk()`: el lote vive en el **núcleo**, no en el llamador, por la
misma razón que F-F.3 comparte `run_inventory_replay` — un lote armado del lado del caller
volvería a separar lo que aplica el confirm de lo que aplica el reintento del panel.

**Medido con el mismo instrumento antes y después** (100 ventas de 10 productos, contando
sentencias en el propio confirm):

| | sentencias SQL en TODO el confirm | envíos al broker |
|---|---|---|
| antes | 671 | 100 |
| después | **87** | **1** |

Lo que se colapsa: el advisory pasa de dos por venta a uno por corrida (es transaccional, así que
retomarlo no agregaba exclusión, sólo sentencias); el `SELECT` de balance por venta pasa a uno
por chunk; los `UPDATE` de balance y producto pasan de uno por venta a uno por **producto**; y
los `INSERT` de movimiento entran por `executemany`. La traza no se toca: sigue habiendo un
movimiento por venta.

**El orden importa y no es estético:** primero el movimiento, después el saldo. El movimiento es
lo único que puede chocar (índice único de `source_event_id`), así que resolverlo antes deja el
saldo calculado sobre lo que *realmente* entró; al revés habría que adivinar cuánto revertir.

**La carrera se paga por lote, no por archivo.** Si una venta en vivo descontó el mismo registro
entre el pre-chequeo y el INSERT, el lote entero se rechaza y se rehace **de a una por el camino
de siempre**: las demás entran igual y la conflictiva cuenta como ya aplicada. Se rehace el lote
completo —no se lo parte— porque la colisión es rara y el camino de a una es el que ya estaba
probado.

**El clamp se replica paso a paso**, no al final: `stock_units` no baja de cero, así que una
venta que pasa por el piso deja las siguientes restando desde 0, y colapsarlo en un solo
`max(0, …)` sobre el total daría otro número justo en el caso que el clamp existe para cubrir.
`current_qty` del balance sigue sin clamp a propósito: ahí un negativo es el dato de que la
historia del archivo no cierra.

**Los avisos al broker dejan de escalar con las ventas.** `events.stock_decreased` sólo encola el
recálculo de score del tenant: emitirlo por venta encolaba 1.187 veces el mismo recálculo del
mismo negocio. Pasa a uno por corrida. `STOCK_ALERT_CREATED` pasa a uno por producto que queda
bajo el umbral — antes un producto con cuarenta ventas que cruzaba en la doceava emitía
veintinueve alertas idénticas.

**Hallazgo colateral, no reparado acá:** el índice `uq_inventory_movements_live_sale_event` lo
crea la migración `20260729_0001` y **sólo en Postgres**; el schema de los tests sale de
`Base.metadata.create_all`, que no lo conoce. Es decir que **el camino de colisión nunca estuvo
cubierto por la suite** —ni antes ni después de este cambio—: en SQLite el INSERT duplicado
entra. El test de la carrera crea el índice con el mismo predicado que la migración para correr
contra la condición real. Declararlo en el modelo ORM es otra discusión (cambiaría el schema de
todos los tests a la vez).

**Aceptación:** ningún archivo se rechaza por ser plano · el orden de aplicación es por fecha y
no por solapa (dos libros con las mismas ventas y las solapas invertidas dan el mismo
resultado) · una hoja sin producto+cantidad no muestra una sola línea sobre inventario · el
caso don pedro sigue en rojo si se rompe el anclaje.

### F-F.4 — el eje deja de ser una pregunta (plan, no entregado)

**Lo que dijo el usuario y gobierna esta sub-fase:** *«los archivos informacional no tienen
razón de ser, porque para eso estamos editando el sistema: para que toda ingesta tenga
movimiento de inventario, que es una de las principales funciones de Véktor»* y, precisando el
alcance: *«se tiene que poder elegir como hasta ahora a qué sección corresponde cada hoja de un
archivo de Excel, pero todo lo que sea compra y venta de mercadería tiene que tener impacto en
el stock»*.

Eso es más fuerte que «cambiá el default» y más acotado que «sacá las preguntas». Lo que
desaparece es **la segunda pregunta**: hoy, además de decir que una hoja es de ventas, hay que
contestar si esas ventas modifican el stock. La primera ya responde la segunda — si la hoja es
compra o venta de mercadería, mueve inventario.

**Sigue eligiendo el usuario, y no se toca:** a qué sección corresponde cada hoja (el
`context_entity` que resuelve `_entity_for`), el mapeo de cada columna, y `stock_treatment`
—apertura vs compra—, que es la pregunta contable de al lado. Como el efecto se deriva de la
entidad **efectiva**, reasignar una solapa a «ventas» hace que esa hoja empiece a descontar: la
sección sigue siendo decisión suya y el impacto en stock deja de ser una segunda pregunta sobre
lo mismo.

#### El modelo nuevo: dos modos derivados y un tercer estado que no es un modo

| Qué contiene la hoja | Qué le pasa al inventario |
|---|---|
| ventas/compras con producto **y** cantidad | `historical_replay` — las compras suman y las ventas restan, por fecha |
| catálogo con cantidad | `current_snapshot` — el archivo declara el saldo absoluto |
| todo lo demás (Gastos_Fijos, Clientes, Proveedores, servicios sin producto) | **no aplica** — no se pregunta, no se informa, no se muestra |

`INFORMATIONAL` y `NO_INVENTORY` desaparecen de `domain/inventory_effect.py`. Y desaparecen de
distinta manera, que es lo que hay que hacer bien: `informational` era una **decisión** que se
elimina; `no_inventory` era un **modo que representaba la ausencia de inventario**, y su
reemplazo no es otro modo sino la **ausencia de valor**. `default_effect_for` pasa a devolver
`str | None`, y la hoja que no mueve unidades simplemente no entra en el dict de efectos.

**Por qué importa la diferencia.** Hoy `_import_projection.effect_for()` cae a `INFORMATIONAL`
cuando no hay dato, y ese «sin dato» significa dos cosas a la vez: «caller viejo que no mandó el
modo» y «hoja que no habla de inventario». Con el dict resuelto por hoja el «sin dato» queda
unívoco, y `_cuenta_inventario` pasa a ser `effect_for(...) is not None` — un booleano que se
lee igual que la pregunta que responde.

#### El payload sin `context_id` tiene que entrar, o el flip no llega a todos

`_inventory_effects` se calcula **sólo si hay `_ctx_mappings`**. Sin ellos el recorder cae a su
default y el archivo no descuenta. **No es «el archivo de una sola tabla»** —la pantalla ya
califica ese caso con `context_id: "table"` desde F-H3.e—: es el summary sin `mapping_contexts`
y el caller de API directa. Si el flip se hiciera sólo en `default_effect_for`, esos envíos
seguirían sin descontar: el agujero exacto de F-H3.e (la regla escrita e inalcanzable), por
segunda vez.

F-F.4 arma un `SheetInventoryProfile` con `context_id=""` —la misma clave que `effect_for` ya usa
para el contexto ausente— a partir de los mapeos planos. El 422 `efecto_de_inventario_sin_hoja`
se conserva para un override que **nombra** una hoja inexistente: eso sigue siendo una decisión
que no se puede honrar.

**Y el replay de ese camino se pide sin filtrar por hoja.** El importador no estampa contexto en
esas ventas (`_ctx_inline` descarta la clave vacía por falsy), así que filtrar por `[""]` no
matchearía ninguna —`_contexto_de` devuelve `__sin_hoja__`— y el confirm dejaría de descontar en
silencio. Se pasa `context_ids=None`, que es «todas las ventas del archivo»: para un archivo sin
hojas identificadas, la misma cosa dicha sin traducir claves.

#### Los overrides legacy no rompen el confirm durante el deploy

Railway y Vercel redespliegan en paralelo y sin orden garantizado, así que durante la ventana un
frontend viejo va a mandar `{"ctx": "informational"}` contra el backend nuevo. Con
`VALID_EFFECTS` reducido eso es un 422 y el confirm se cae.

`"informational"` y `"no_inventory"` se aceptan como **alias legacy que se descartan** (la hoja
toma su modo derivado) con `logger.info`. No es una concesión: es la traducción exacta de la
decisión del usuario —esos valores dejaron de ser decisiones—, así que descartarlos no pierde
ninguna intención que siga existiendo. Un modo desconocido de verdad sigue siendo 422.

#### Sub-commits

- **a · el dominio pierde los dos modos.** `inventory_effect.py`: dos constantes, `Literal` de
  dos, `EFFECT_LABELS` de dos (y el de replay deja de sonar a opción: describe lo que va a
  pasar). `default_effect_for → str | None`; `options_for` deja de ofrecer y pasa a explicar.
  Se reescriben `test_historical_replay_nunca_es_un_default` y el docstring del módulo
  **declarando por qué se levanta la regla** —el ancla se aplica antes de todos los eventos y el
  replay ordena por fecha, que son las dos condiciones que en don pedro no existían—, y se
  conserva la compuerta del doble descuento: si alguna de las dos piezas se cae, el default
  tiene que volver.
- **b · el confirm aplica siempre que la hoja mueva unidades.** El mecanismo ya está (F-F.3
  aplica dentro del savepoint, F-F.3.b lo hace por producto y no por venta): lo que cambia es
  **cuántas hojas caen ahí**. Entra el camino plano. Se revisan los avisos que todavía mandan al
  usuario a aplicar desde el panel algo que el confirm ya aplicó.
- **c · la pantalla deja de preguntar por el efecto** (el selector de SECCIÓN queda igual).
  `InventoryEffectChoice` pasa de selector a línea informativa; la hoja sin efecto no muestra
  **nada** (hoy muestra el cartel que el usuario pidió sacar). `/inventory-effects` sigue
  existiendo y sigue siendo la única fuente —la regla es de dominio y una copia en la UI se
  desactualiza (el defecto ya pagado con el catálogo de campos)—, pero pasa a explicar en vez de
  ofrecer. La respuesta se mantiene **aditiva** (`options` con un elemento o vacío) para que un
  frontend viejo muestre la línea correcta durante la ventana de deploy en lugar de romperse.
  `ColumnMapperPanel` **elimina** el estado de elección (`effectByCtx`, `efectoDe`, el
  `effectElegido` del camino plano) y deja de mandar `inventory_effect`; no se toca la
  preservación de mapeos de F-A (`60d400f8`), que vive en otro estado.
- **d · la relectura descuenta.** `reread_service` llamaba a `insert_confirmed_data` **sin efecto
  de inventario**, así que re-importaba ventas que no descontaban — y eso no era neutral: el void
  previo revierte todo movimiento vivo del archivo (incluidos los `sale` del replay del confirm),
  así que **releer DEVOLVÍA el stock descontado**. Después del reimport corre
  `run_inventory_replay`, la misma función que el confirm y el panel.
  - **El efecto se DEDUCE de lo que la relectura acaba de leer**, no del que guardó el confirm.
    Lo pidió el usuario y es la razón de ser de la relectura: *«tiene que poder modificar si
    detecta variaciones de cantidades, o registrar gastos o ventas si las hay y previamente no
    fueron detectadas; también puede darse el caso de que no lea algo diferente»*. Con el dict
    viejo, una cantidad recién detectada entraría **sin mover stock** — el dict no la conoce—, o
    sea una venta de mercadería que no descuenta, justo lo que F-F.4 elimina. Deducirlo de nuevo
    también es lo consistente: el efecto es consecuencia del contenido, y acá el contenido se
    volvió a leer. `_deduce_inventory_effect` arma los mismos `SheetInventoryProfile` que el
    confirm sobre `derive_context_mapping_entries` —la misma derivación que gobierna esa
    importación— y los resuelve con la misma función; falla blanda a `{}` (no descontar).
  - **Consecuencia elegida por el usuario:** un archivo importado ANTES de F-F.4, cuyas ventas
    nunca descontaron, **queda al día en cuanto se lo relee**. Se descartaron «avisar antes» y
    «sólo lo nuevo que detecte». También hace innecesario el sub-commit **e** para el caso más
    común: releer alcanza para cerrar un archivo viejo.
  - El efecto resuelto se sigue persistiendo en `parsed_summary_json` al confirmar (al lado de
    `stock_treatment`), como traza de con qué entró el archivo — ya no como fuente de la
    relectura.
  - **El alcance sale de `replay_scope`** (`domain/inventory_effect`), no de un filtro reescrito
    en cada llamador: son dos, y la traducción de la clave vacía —la del archivo sin hojas
    identificadas, que se aplica entero— es justo lo que el segundo habría implementado distinto.
  - **El orden no es estético:** el replay va ANTES del bloque que audita los movimientos nuevos
    como `REREAD_INSERT`. Puesto después, el descuento quedaría fuera del `DataRepairItem` y el
    undo dejaría el stock descontado sin las ventas que lo justifican. Hay un assert que lo fija.
  - **Bug encontrado por el test de la fila editada a mano (V28).** La relectura preserva esa
    fila, pero su movimiento de descuento no lleva `source_row_ref` —lo identifica
    `source_event_id`— así que el guard de preservación no lo protegía: se voideaba, y quien
    tenía que restituirlo era el replay posterior. Eso sólo funciona si el filtro por hoja del
    replay alcanza a esa venta, **y no la alcanza**: la venta preservada conserva el sello del
    import ANTERIOR y la relectura deduce sus hojas de nuevo. Resultado: la venta editada se
    quedaba en los libros y sus unidades volvían al stock. Se arregla protegiendo el movimiento
    con la misma regla que la fila (`preserved_sale_events`), en vez de hacer que la reversa
    dependa de que dos derivaciones distintas coincidan.

  ⚠️ **Hallazgo colateral, NO reparado acá:** la relectura re-importa las transacciones **por
  autodetección** — a diferencia de los maestros, que sí conservan su mapeo
  (`master_column_mappings`), el mapeo de columnas de ventas/gastos no se persiste. Un archivo
  importado con un mapeo explícito puede re-importarse resolviendo otras columnas, y desde F-F.4
  eso además mueve stock. Es previo a esta fase; se documenta acá porque la fase le sube el
  precio. El test de F-F.4 lo esquiva a propósito (usa un CSV que el importador autodetecta) para
  medir el descuento y no la autodetección.
  **La reversa ya existe y hay que probarla, no asumirla:** la relectura voidea todos los
  movimientos vivos con `source_upload_id == file_id` (`:748-760`), y los del replay lo llevan
  (`inventory_replay_service:304`), así que se revierten. El caso a fijar con test es la venta
  **editada preservada**: su movimiento no lleva `source_row_ref`, así que el void no lo saltea,
  y quien lo tiene que restituir es el re-apply por idempotencia de `source_event_id`. Relectura
  ×N = mismo stock, o el descuento se pierde justo en las filas que el usuario corrigió a mano.
- **e · los archivos que quedaron sin aplicar.** Sus ventas están importadas y no descontadas, y
  la vía para cerrarlos existe (`POST /ingestion/files/{id}/inventory-replay`), pero el panel
  sólo aparece en el flujo post-confirm de la sesión: un archivo confirmado la semana pasada hoy
  no tiene por dónde. Se expone el impacto pendiente desde la lista de archivos, reusando el
  `dry_run` que el panel ya usa para descubrir hojas. **Sin backfill automático:** aplicar mueve
  stock, y hacerlo en bloque sobre todos los archivos históricos de todos los tenants es
  literalmente el movimiento que dejó la lección de don pedro. Lo aplica el usuario, archivo por
  archivo, viendo el número.

#### La venta sin respaldo sigue yendo a «Otros» (decisión del usuario, 2026-08-12)

El flip tiene una consecuencia que el plan no había anticipado: el gate de F-H3.d pasa a correr
para todos. Una venta del 10/03 cuyo único respaldo es una compra del 20/03 dejó de importarse
con el aviso `historial_insuficiente` — va a «Otros». Eso ya pasaba, pero sólo si el usuario
elegía el replay a mano.

Consultado, el usuario **ratificó lo que había decidido en F-H3.d**: el stock no queda negativo
y la fila no entra como venta; se completa el inventario y se registra desde «Otros». Se
descartaron las dos alternativas (importarla con el descuento pendiente, o hacerlo depender de si
el archivo declara saldo). Queda declarado el riesgo que acompaña a la decisión: un archivo
histórico de un negocio que nunca registró sus compras viejas cae entero a la bandeja.

`historial_insuficiente` no muere: sigue vivo para las hojas que no mueven unidades (una venta
con producto pero sin columna de cantidad), que son las que no pasan por el gate.

#### Lo que NO cambia (para no re-litigarlo)

El ancla del catálogo sigue sin fecha y se aplica **antes** de todos los eventos: es la
condición que habilita el flip, no un detalle · las compras suman en cualquier modo (V16) ·
`stock_treatment` sigue siendo el eje contable, separado · una venta sin respaldo sigue yendo a
«Otros» (F-H3.d) o quedando pendiente y contada (F-F.2) · `inventory_integrity_service` **no
cambia de lógica**: decide por el ledger, no por el modo; sólo su docstring nombra
`informational`.

**Aceptación:** un archivo de UNA hoja de ventas con producto y cantidad descuenta stock al
confirmar sin que el usuario toque nada · ninguna pantalla dice «no afecta el inventario», y la
hoja de Gastos_Fijos no muestra ninguna línea de inventario · un confirm con `"informational"`
en el payload no se cae · relectura ×2 = mismo stock que ×1, incluidas las filas editadas ·
`default_effect_for` no devuelve modo para una hoja que no mueve unidades · el caso don pedro
sigue en rojo si se rompe el anclaje o el orden por fecha.

---

# F-A · Preservar primero, clasificar después

Cada columna arranca visible **con su nombre original**.

- Reconocida → destino canónico propuesto. No reconocida → entidad de la hoja + `custom_field:<slug>` + label = header original.
- **El slug se genera en backend** (`custom_field_slug()` en `app/domain/header_keys.py`): es un identificador persistido (`tenant_custom_field_definitions.field_key`, `String(80)`). **No reusar `_normalize_col`** (`column_mapping_service.py:317-325`), que alimenta `tenant_column_mappings.source_column`. Reglas: normalizar acentos → `[^a-z0-9]` a `_` → colapsar → prefijo `c_` si arranca con dígito → truncar a 72 → **vacío devuelve `None`**.
- El **label legible viaja separado** (`target_label`); no se reconstruye desde el slug.
- Slugs duplicados: desambiguación determinística por orden de aparición (`obs`, `obs_2`) **más** la validación dura de F-0. Nunca last-wins.
- El backend **canoniza todo `custom_field:` que llega** en el confirm, antes de `_dropped_pairs`, `validate_column_risk_decisions`, `ensure_custom_field_exists:1683` y `_trae_maestros:1799`. **Grep obligatorio de `body.column_mappings` antes de cerrar.**

**Cambiar la sección conserva** texto editado, label, decisión manual, intención de ignorar y custom field original; sólo se recalculan las sugerencias **no tocadas**. Hoy se pierde todo (`ColumnMapperPanel.tsx:420-429`) y con F-A se perdería el mapeo de 20 columnas: el fix entra acá.

**Columnas vacías, tres capas:** no proponer sin muestra en `sample_vals` · proponer no crea nada (`ensure_custom_field_exists` corre en el confirm) · el confirm dropea las 100 % vacías del archivo completo (`column_risk.compute_column_null_stats` ya recorre los buckets enteros) salvo `user_selected=True`.

**Corrección obligatoria de `required_missing` (V10):** cobertura **sólo con targets canónicos**; un custom field homónimo no cubre un requerido; se valida antes y después de materializar los defaults custom; el estado pertenece al **campo destino faltante**, no a una columna arbitraria.

---

# F-B · Mapeo rápido y comprensible

**Paso 0 (refactor puro):** extraer `TargetSelect` y `MappingOriginHint` del monolito de 1817 líneas de `ColumnMapperPanel.tsx`, hoy duplicado en el flujo multi-hoja (`:661-682`) y el plano (`:1657-1667`). Sin esto, F-A, F-B y F-D tocan dos lugares cada una.

**Fuera el porcentaje.** No es una probabilidad calibrada: `heuristic` es `0.75` hardcodeado y `fuzzy` es `ratio × 0.65` con techo 65 % sobre piso 0.70 → **ningún fuzzy puede superar a ningún heurístico**. Se conserva en `title=` y en el payload (lo consume `column_risk.MappingEntry`).

**Procedencia, una línea:** "Lo elegiste vos" · "Usado antes por tu negocio" · "Sugerido por el nombre" · "Sugerido por los valores" · "Se guarda con el nombre del archivo" · sin sugerencia → nada. `mappingOrigin(s, target)` en `mappingRules.ts` compara el target actual contra el sugerido (no un flag `touched`: tocar y volver al mismo valor no debe mentir).

**Orden del selector:** frecuentes/recomendados → campos de la sección actual → otras secciones permitidas → guardar como campo propio → ignorar.

**Acciones masivas** (lo que realmente baja el tiempo): aceptar todas las sugerencias visibles · conservar todas como campos propios · ignorar columnas vacías · aplicar una decisión a columnas equivalentes. Más muestras de valores visibles, navegación por teclado y búsqueda en el selector.

**Métrica:** tiempo mediano de archivo analizado → confirmación, segmentado por cantidad de columnas.

---

# F-C · Obligatorios explicados por el dominio

Extensión aditiva del catálogo (`GET /ingestion/field-catalog`), con el motivo del lado del backend porque **es consecuencia de una regla del importador, no una opinión de UI**:

```json
{ "value": "transaction_date", "label": "Fecha de venta", "required": true,
  "required_reason": "Permite ubicar la venta en el período correcto." }
```

Copy: **"Para importar ventas, Véktor necesita saber qué columna contiene la fecha"** — no "esta columna es obligatoria". La queja nace de leer `Campos requeridos sin mapear: transaction_date`, que suena a que la columna es obligatoria; es al revés. El banner lista campos faltantes y **salta al selector correspondiente**.

**"Obligatorio" es contextual** → `required: bool` evoluciona a reglas declarativas, conservando el booleano por compatibilidad (`missingRequiredFields` tiene tests):
- `sale.amount` es obligatorio **salvo** que `unit_price + quantity` lo calculen (F-H4).
- Producto es obligatorio **sólo** para una venta inventariable (F-H1).
- Cantidad es obligatoria **sólo** si la hoja mueve inventario (F-H3).

El 422 pasa a usar labels humanos (`CANONICAL_FIELDS[entity][field]`), como ya hace `_collision_detail`.

**Test:** todo campo de `REQUIRED_FIELDS` tiene motivo no vacío. El copy no puede afirmar lo que el importador no hace: sin fecha la fila va a /otros, **sin monto se descarta** (`:4186-4187`).

---

# F-O · «Otros» y la relectura

**Lo que pidió el usuario:** *«toda venta o compra que haya caído a Otros y fue redirigida a
alguna sección de Véktor, al realizar relectura también tiene que modificarse»*.

**Lo que se midió antes de diseñar** (sonda sobre un CSV de dos filas, la segunda con la fecha
ilegible): no es que no se modifique — **la relectura la BORRA**. La venta clasificada a mano
queda anulada con `REREAD_REIMPORT` y nadie la repone: para el parser esa fila sigue sin poder
leerse (por eso había caído a «Otros») y su `UnclassifiedRecord` ya está en `IMPORTED`, así que
tampoco vuelve a la bandeja. Se pierde el trabajo del usuario **y** el dato. Es previo a F-F.4.

**Causa:** el registro nace con `source_row_ref = "unclassified:{id}"` y `has_user_edits=False`,
así que `_split_records` lo manda al bucket «no editado» → void + esperar que el reimport lo
reponga. El reimport no puede reponerlo, y ese ref **no corresponde a ninguna fila del archivo**:
el camino exacto de la reconciliación no tiene con qué emparejarlo.

**Decisión del usuario para el caso en que la relectura SÍ pueda leer la fila:** gana la
relectura — el registro se actualiza con lo que dice el archivo. Se descartaron «gana lo tuyo» y
«gana la relectura salvo los campos que editaste».

## F-O.1 — dejar de perder el dato (entregada)

El registro nacido de «Otros» se preserva, por la misma razón que una fila editada a mano: es una
decisión humana sobre una fila que el archivo no explica solo.

- **La identidad del ref se extrajo a un lugar único** (`models/unclassified_record`:
  `UNCLASSIFIED_ROW_REF_PREFIX` + `unclassified_row_ref()` + `is_unclassified_row_ref()`). Estaba
  escrita a mano en tres lugares —el que la estampa, el borrado por procedencia y ahora la
  relectura—, y su desacuerdo no da error: da comportamiento distinto.
- **Los dos motivos de preservación se preguntan desde una sola función** (`_se_preserva`). Cada
  guard que lo re-derive por su cuenta puede quedarse con la mitad: es exactamente lo que pasó
  con el movimiento de la venta editada (V28), así que el guard del `InventoryMovement` usa la
  misma.
- **Se cuentan aparte** (`preserved_from_others`): preservar por edición y preservar por
  clasificación son dos motivos, y el informe tiene que poder decir cuál.

**Límite que tenía F-O.1 sola:** si la relectura ahora sí sabía leer la fila, la importaba
**además** y quedaban las dos. Lo cierra F-O.2.

## F-O.2 — que la relectura la modifique (entregada)

El vínculo fila↔registro se persiste al capturar: `ROW_REF_KEY` (`__row_ref__`) en `row_data`
guarda el `source_row_ref` que le habría tocado a la fila, o sea **la clave exacta con la que el
reimport la insertaría**. Aditivo, sin migración, y bajo el prefijo `__` que `/otros` ya oculta
del render (mismo criterio que `__risk_ref__` de F8). Se guarda el ref ya derivado y no sus
componentes: recomputarlo del otro lado sería una segunda derivación que puede quedar distinta.

**La pieza que faltaba no era el vínculo: era la huella.** Medido — con el vínculo puesto, la
fila seguía sin re-importarse. La captura a «Otros» es output persistido y **registra su huella
de idempotencia**, así que el reimport salteaba esa fila para siempre y la pregunta «¿ya la sabés
leer?» no llegaba a hacerse. Se libera antes del reimport, que es el mismo movimiento que F8 hace
con las filas de riesgo corregidas (`_reconcile_column_risk` borra su huella para que entren en
el mismo reimport).

Secuencia: **liberar la huella → el reimport procesa la fila → resolver**. Si entró, el registro
clasificado se anula (gana la relectura) con su movimiento de stock y su auditoría. Si no entró
—sigue ilegible y volvió a «Otros»— esa captura nueva se marca `DISMISSED`: la clasificación del
usuario sigue mandando, y pedirle que clasifique dos veces lo mismo es ruido. Liberar la huella
no cuesta nada por sí solo, justamente por esa segunda mitad.

**El cableado salió barato:** `_add_sale`, `_add_expense`, `_add_product` y el merge de catálogo
**ya recibían el ref de la fila**, así que ninguno de los 18 call sites cambió de firma; 15
recibieron el argumento y 3 se quedan sin ancla (tabla sin clasificar, documento de texto sin
fecha, hoja entera sin clasificar) y degradan a F-O.1 — se preservan, que no pierde nada.

**V29 — bug propio, introducido por F-F.4.d y encontrado acá.** El bloque que audita «los
movimientos nuevos» tomaba TODO movimiento vivo del archivo, con el comentario «tras el void
anterior, cualquier movimiento vivo es nuevo». Dejó de ser cierto en cuanto el void empezó a
PRESERVAR movimientos (V28 y F-O.1): quedaban vivos sin que la relectura los hubiera creado, se
auditaban como inserción y **el undo los anulaba** — devolvía stock que la relectura nunca tocó y
dejaba la venta viva sin su movimiento. Se excluyen explícitamente, con test propio.

---

# Paso 0 · Medir antes de tocar (compuerta)

Mismo principio que F-T: no se le agrega trabajo —ni se le saca dato— a algo que no se midió.
Read-only, sobre `backend/scripts/diag_asteria_import.py` (sólo SELECT, nunca imprime la URL):

1. **«Otros»**: distribución por `context_label` × archivo; de los pendientes, cuántos tienen TODOS
   los valores vacíos, cuántos sólo espacios o caracteres invisibles (`strip()` vacío con longitud
   > 0), y cuántos son `"Tabla sin clasificar"` **con** contenido real.
2. **Identidad**: ventas con `product_id IS NULL` y —lo que importa— **cuántos NOMBRES distintos**
   hay entre ellas: ése es el tamaño real del trabajo de F-S.0, no la cantidad de filas.
   Distribución de `custom_fields._customer_resolution`, de `products.category` y de SKU
   presente/ausente.
3. **Duplicación**: `scripts/dedupe_products_by_name.py --tenant <uuid> --out plan.csv` (dry-run, ya
   persiste el plan sin tocar negocio) y su `coverage()`.

**Es compuerta:** la limpieza de F-O.4 y los backfills de F-S y F-CAT no se ejecutan antes de leer
esta salida. Si la distribución contradice la hipótesis, se rediseña la fase — no se fuerza.

## Lo que midió el Paso 0 sobre ASTERIA (2026-08-14) — y qué cambió

Corrida real contra Neon. **Un solo archivo** (`ASTERIA_home_deco.xlsx`) es el origen de todo, con
cabecera de catálogo `Tienda · Productos · Especificaciones · Stock · Precio de compra · % Envio ·
compra+envio · Precio de lista · col_8 · Precio de venta final`.

**1. «Otros» — hipótesis refutada.** Ver F-O.4: no eran filas en blanco (13%), eran dos hojas
enteras (99,6%). La fase se rediseñó y el peso pasó a F-O.3.

**2. Clientes y proveedores: no hay dato que identificar.** `_customer_resolution` da `anonymous` en
**1939 de 1939** ventas. `anonymous` no es "no matcheó": es que la fila **no traía ninguna referencia
de cliente**. Y `supplier_name` es NULL en las 624 filas de gastos. **Consecuencia para el plan: ni
F-I ni F-E van a hacer aparecer un cliente en ASTERIA** — falta el dato, no el código. Prometer lo
contrario sería vender una fase por un defecto que no arregla. Lo que sí corresponde: que la pantalla
**diga** que todas las ventas son de mostrador porque el archivo no trae cliente, en vez de mostrar
«Local» en 1939 filas como si algo hubiera fallado.

**2-bis. La excepción: `Tienda` ES el proveedor.** Confirmado por el usuario: la columna dice dónde
compra cada producto. O sea que el archivo **sí** trae proveedores, en la hoja de catálogo — el único
lugar del que hoy está prohibido sacarlos. Ver F-E.2.

**3. Productos: 0 de 398 con SKU, 0 con código de barras, 0 con categoría.** Confirma el orden
F-CAT → F-S (si se numera antes, los 398 nacen `GEN-xxxx`). Y el archivo **no tiene columna de
categoría**: para ASTERIA la rama "mapear" de F-CAT no tiene nada que mapear, queda sólo la
inferencia por nombre con evidencia.

**4. Ventas: 1403 de 1939 (72%) sin producto, sobre 1128 nombres distintos.** Ese número mata el
diseño de "cola agrupada por nombre" tal como estaba: 1128 decisiones a mano no son una cola. Y en el
propio top aparecen `alfombra felpuda exterior` (6) y `alfombra felpudo exterior` (4) — el mismo
producto escrito de dos formas, que el tier de tokens no une (`felpuda` ≠ `felpudo`). Por eso el
diagnóstico incorporó la **forma de la cola**: clasifica cada nombre con el motor real en `exacto` /
`token único` / `varios candidatos` / `sin candidato`. La distinción decide la fase: lo que cae en
`exacto`/`token único` **el motor de hoy ya lo resolvería**, así que estar sin vincular sería un bug
de linkeo (se arregla re-resolviendo, sin trabajo humano); sólo `varios candidatos` es cola real, y
`sin candidato` no se vincula ni se adivina. **F-S.0 no se termina de diseñar hasta leer ese corte.**

---

# F-R · La relectura tiene que probar su correspondencia

**El síntoma.** La relectura de ASTERIA ofrece `2563 a actualizar / 2563 a anular / 0 preservados /
6 nuevos`, y lo primero que muestra son tarjetas «Anulado → Después: —» sueltas.

**Lo que se midió antes de escribir la fase:** los dos 2.563 son **las mismas filas**. En
`reread_service.py:1673-1677`, `to_void = len(recon.non_edited)` y `to_update` cuenta las filas
frescas cuya huella está entre las que se van a anular — anular y reimportar corregido *es* el
mecanismo de actualizar. En esa corrida no hay pérdida neta. **El defecto no es el número: es que
nada lo garantiza y la pantalla no lo explica.**

- `to_update` sale del parse **nuevo**. Si el archivo cambia de clasificación —el escenario del bug
  vivo de `has_catalogo_fuerte`— o cambia el mapeo, `to_void` se queda en 2.563 y `to_update` cae a
  cero. **Hoy nada frena ese apply.**
- El orden de `sample_changes` (`void_samples + update_samples + new_samples`, `:1667`) muestra las
  anulaciones primero y sin su contraparte: el usuario ve destrucción donde hay reemplazo.

**Qué entrega la fase:**
- **Correspondencia explícita** por `source_row_ref` (hoja + índice de fila): cada registro a anular
  se clasifica en **reemplazado / preservado / sin reemplazo**. Los legacy sin ref no se pueden
  emparejar — son la excepción honesta que ya señala `legacy_fallback` y se cuentan aparte, nunca
  disfrazados de reemplazados.
- **Compuerta antes de aplicar:** con `sin_reemplazo > 0`, el apply se bloquea con el número y la
  entidad de lo que desaparece, y exige aceptación explícita. Anular sin reponer es legítimo (el
  archivo cambió de verdad); hacerlo **en silencio** no.
- **Conteos por entidad, antes/después** — ventas, gastos, productos, clientes y proveedores. Hoy
  sólo se estima el impacto en productos (`_estimate_products`).
- **El preview deja de asustar de más:** se muestra el par antes/después de la MISMA fila, y la
  tarjeta «Anulado / Después: —» queda reservada para lo que de verdad no tiene reemplazo.

**Aceptación:** releer el mismo archivo sin cambios da correspondencia 1:1, cero pérdida neta y cero
duplicados · un archivo cuya hoja cambia de clasificación **no puede aplicarse** sin aceptación
explícita · los conteos por entidad cuadran antes y después.

---

# F-S.0 · Catálogo y transacciones se vinculan en la MISMA carga  ✅ ENTREGADO (2026-08-14)

**Entregado en 4 commits** sobre `feat/ingestion-identity-reread-safety`, cada uno un mecanismo:
`2191dbe5` (sku/barcode target de venta), `4063b33c` (barcode en el índice transaccional
same-file), `936e3c9d` (alias persistido), y el de la cola de ventas sin producto (`counts` +
warning + `GET/POST /sales/product-link-queue`). Plan ejecutable con el detalle TDD completo:
`docs/superpowers/plans/2026-08-14-f-s0-product-link.md`.

**Lo que cambió respecto del plan original, medido durante la implementación (no en el diseño):**
- **La heurística de encabezado real es `RESOLUCION["sale"]`, no `_HEURISTICS`.** El reconocedor
  F-M (`read_header`/`analyze_header`) es la capa PRIMARIA que usa `suggest_mappings` en
  producción; `_HEURISTICS`/`_heuristic_match` es sólo el fallback fuzzy y, medido con grep, ni
  siquiera lo llama código de producción — sólo tests directos. Se agregó a los dos por
  completitud, pero el que importa es `RESOLUCION`.
- **Mecanismo 2 (barcode same-file) era un gap de 3 líneas, no una fase nueva.** F-H1 ya
  resolvía sku/nombre same-file (`_register_product_transaction_indexes`); sólo faltaba
  propagar barcode a los 3 call sites. Verificado además con las hojas en orden FÍSICO inverso
  en el archivo — la garantía la da `_orden_de_pasada` (`product:0, expense:1, sale:2`), no el
  orden del Excel.
- **La lógica de ambigüedad del borrador de alias tenía un bug real** (encontrado en revisión,
  no en producción): iterar `(nombre, *alias)` con `by_name[norm] = pid if norm not in by_name
  else None` marca a un producto ambiguo CONSIGO MISMO cuando su alias normaliza igual que su
  nombre. Corregido replicando el patrón ya probado de
  `_register_product_transaction_indexes:1766-1769` (comparar contra el `pid` de la iteración,
  no sólo contra "la clave ya estaba ocupada").
- **La cola de vinculación (mecanismo 4) se endureció bastante sobre el borrador**, con la
  guía de una revisión previa a implementar: `GET/POST /sales/product-link-queue` van
  DECLARADAS ANTES de `GET /{sale_id}` (si no, Starlette matchea ahí y 422 en vez de 200); el
  POST marca `has_user_edits=True` (mismo guard que el `PATCH` manual, para que F-R/F-F no pisen
  la vinculación en una relectura); una fila de auditoría AGRUPADA por operación
  (`SALES_PRODUCT_BULK_LINKED`, no una por venta); `trigger_score_recalculation.delay(...)`
  (Celery, no la función síncrona); `ensure_tenant_not_under_maintenance` +
  `maintenance_lock_service.acquire_write_lock_shared` (muta `Product.custom_fields`, mismo
  chokepoint que el resto del catálogo); escaneo paginado por `id` con dos topes independientes
  (filas que califican vs. filas escaneadas) que reportan `truncated` en vez de cortar en
  silencio; candidatos sugeridos por grupo (misma forma `{id, matched_by, name, sku, barcode}`
  que `match_candidates` de "Otros", sin reusar el motor privado de identidad del import).
- **El warning del confirm no promete una pantalla que no existe todavía**: "no encontraron su
  producto... quedaron pendientes de completar", sin mencionar una acción/cola clickeable —
  el frontend de la cola queda fuera de este alcance (ver abajo).

**Fuera de alcance, a propósito:** frontend de la cola (mismo patrón de secuenciación que F-O.3
en este programa: backend completo primero, ver ese apartado). El endpoint existe y está
testeado; la pantalla que lo consuma es un fast-follow.

**Deuda declarada, encontrada por `/code-review high` sobre el diff final:** el aprendizaje de
alias (mecanismo 3) sólo se escribe desde `POST /sales/product-link-queue/link` — la rama
"vincular a producto existente" de `others.py::reclassify_record` (F2-T2b, la misma acción para
filas de "Otros") es estructuralmente idéntica pero NO llama a `add_alias`. Un tenant que
resuelve sus ambigüedades desde "Otros" en vez de la cola nueva no se beneficia: el mismo nombre
crudo va a fallar de nuevo en la próxima importación. No se resolvió en esta entrega porque
`others.py` no tiene ahí un string de "nombre crudo" limpio como el que sí tiene la cola —
`UnclassifiedRecord.row_data` es la fila cruda completa, no un campo extraído — y forzar la
extracción sin plan propio bajo presión de tiempo era más riesgo que beneficio. Candidato a
fase corta propia antes de F-S.

**El síntoma.** En `/sales` de ASTERIA la columna Producto está casi toda en «—»: de ocho ventas
visibles, dos linkearon. Y el catálogo tiene 398 productos.

**Lo que se midió:** `CANONICAL_FIELDS["expense"]` tiene `sku` y `barcode`
(`column_mapping_service.py:77-78`); **`CANONICAL_FIELDS["sale"]` no tiene ninguno de los dos**
(`:35-55`). Una compra puede declarar la identidad del producto que compra y una venta no: lo único
que le queda a una venta es el nombre. Ésa es la raíz, y **ningún SKU generado después la arregla**
—por eso F-S.0 va antes que F-S—.

Cuatro mecanismos, en orden de fuerza:

1. **El código que el archivo ya trae.** `sku` y `barcode` como targets de `sale`, con su heurística
   de encabezado. El resolvedor ya los prioriza (`_resolve_product`, tier barcode → sku → nombre →
   tokens): falta el target, no el motor.
2. **Referencia exacta entre hojas.** Cuando el archivo trae catálogo y ventas juntos, el código de
   la venta resuelve contra los productos que ese archivo declaró, no sólo contra la base. El orden
   maestro→transacción (F7c) y el índice por corrida (`_load_product_identity_indexes`) ya existen.
3. **Alias explícito, persistido.** Cuando el usuario vincula a mano un nombre a un producto, ese
   nombre queda como alias del producto (`custom_fields["_aliases"]`, sin migración) y entra al
   índice `by_name` de las corridas siguientes. Sin esto cada import repite el mismo trabajo manual.
   El alias es del tenant y **sólo lo crea una decisión humana** — no se infiere.
4. **Cola de revisión agrupada por NOMBRE, no por fila.** Una venta que no resuelve producto hoy
   queda con `product_id = NULL` en silencio. Pasa a contarse (`counts["ventas_sin_producto"]`) y a
   ofrecerse para vincular agrupada por nombre distinto: 2.563 ventas son N nombres, y resolver N
   nombres una vez arregla las 2.563. Reusa `match_candidates` + el VINCULAR de «Otros» (F2-T2b).

**Límite declarado:** sin código y sin nombre suficiente, Véktor **no adivina** — la venta queda sin
producto, contada y visible en la cola. Misma regla que las fechas (F6-A2), las filas sin monto
(F-H4) y el envío sin comprobante (F-H6.b).

---

# F-ID · Identidad transversal en tres capas (Producto / Cliente / Proveedor)

**Reemplaza a F-S y a la mitad de F-I** (2026-08-14, ampliación pedida por el usuario tras revisar
un primer borrador de F-S en aislamiento). El texto completo — tres capas, esquema, resolvedor,
tareas ID.0–ID.11 — vive en `docs/superpowers/plans/2026-08-15-f-id-entity-identity.md` (persistido
apenas se dejó de reescribir sólo en chat, ver `[[feedback_persist_plans_to_file]]`). Resumen:

**Tres capas, no una.** (1) UUID interno (`Product.id`/`Customer.id`/`Supplier.id`) — ya existe, ya
es la FK de todo, no se toca. (2) Código Véktor permanente, uno por entidad (`PREFIJO-NNNN`,
`products.sku` para producto —decisión ya cerrada, sin migración—, columna nueva `vektor_code` para
cliente/proveedor). (3) `entity_identifiers` — tabla transversal, una entidad puede tener VARIOS
códigos externos de fuentes distintas (namespace `business`/`vektor`/`supplier:<id>`), con
procedencia, sin colisionar entre fuentes que reusan el mismo valor crudo. La razón de la capa 3:
generar sólo `CLI-0001` no ayuda a vincular un archivo que nunca conoció ese código — hace falta
recordar también los códigos que SÍ trae cada archivo.

**No-reciclo estructural:** secuencia atómica (`entity_code_sequences`, `UPDATE...RETURNING`, nunca
un `MAX`+reintento) + fila permanente insert-only en `entity_identifiers` (nunca se borra, ni al
desactivar ni al fusionar la entidad).

**Backfill nunca saltea por ambigüedad:** toda entidad real recibe código, ambigua o no —dos
proveedores de igual nombre reciben `PRV-0012`/`PRV-0013` cada uno— y la detección de posibles
duplicados es un paso aparte que marca para revisión humana, nunca fusiona sola.

**Absorbe la mitad de F-I** (la migración `external_code` que F-I proponía queda reemplazada por
`entity_identifiers`) y **deja el resto de F-I intacto, ahora más chico**: la jerarquía del
resolvedor y su wireo en `_classify_row_reference`/`_resolve_product_identity` son la tarea ID.7 de
F-ID, no una fase aparte.

**Aceptación:** toda entidad real tiene código Véktor y nunca se recicla · ningún código del
negocio se pisa · un identificador externo conserva quién lo trajo y cuándo · dos identificadores
fuertes contradictorios en la misma fila dan `conflict`, nunca gana el primero · fusionar transfiere
identificadores, nunca los pierde · re-importar el mismo archivo no duplica maestros.

**Entregado completo (ID.0–ID.10, 2026-08-15):**
- **ID.8** (`5d4eaccd`) — `product_dedup_service._apply_one_group` re-apunta las filas vigentes de
  `entity_identifiers` del duplicado al canónico (paso 4b, antes de desactivar). Deuda declarada:
  `revert_dedup_run` (T6) no revierte la transferencia — reactivar un duplicado fusionado no le
  devuelve sus identificadores; benigno (no rompe el revert, sólo no lo completa), cubierto por test.
- **ID.9** (`7f82e8cb`) — `vektor_code` visible en `/customers`+`/suppliers` (columna oculta por
  default + ficha de detalle), mismo patrón que el SKU de producto. Búsqueda por código exacto salió
  gratis: `SmartTable` ya busca sobre columnas ocultas.
- **ID.10** (`dc0a9f90`) — `get_entity_ref()` (`agents/shared/entity_ref.py`), helper de sólo lectura
  `{id, code, display_name}` para que un agente con un UUID ya resuelto lo muestre con su código
  ("Juan Pérez (CLI-0042)"). No wireado a ningún agente todavía a propósito — ninguno tiene hoy un
  caso de uso concreto (decisión ya tomada arriba).
- Regresión detectada al correr la suite completa tras cerrar ID.8-10 (no causada por ellos, sino por
  ID.5): `test_catalogo_sin_marca_persiste_custom_fields_vacio_no_null` asertaba `custom_fields == {}`
  literal — dejó de ser cierto porque un producto auto-numerado ahora trae
  `custom_fields["_sku_origin"]="vektor"`. Fix en `492f3d19`: el test ajustó la aserción a la forma
  correcta (sigue cubriendo el bug real, `null` vs dict) y se renombró.

---

# F-CAT · Categorías: mapear, normalizar, inferir con evidencia, backfillear

**El síntoma.** En `/products` la columna Categoría está casi toda en «—», y el desplegable ofrece
Textiles, Iluminación, Muebles… que no devuelven nada.

**Lo que se midió:** la normalización ya existe — `normalize_product_category`
(`domain/product_categories.py:374`) resuelve alias por vertical más los labels canónicos, con
fallback `OTHER`, y el importador la aplica cuando la hoja **trae** columna de categoría. Lo que
falta es que el producto creado desde una línea de compra nace con `category=None`
(`build_incomplete_product`) y nunca se completa.

1. **Mapear** — la línea de compra que crea el producto le pasa la categoría de esa fila.
2. **Normalizar** — reusar `normalize_product_category`. No se escribe un segundo normalizador; si
   faltan alias reales del rubro, se agregan a la tabla curada.
3. **Inferir sólo con evidencia** — desde el NOMBRE del producto, y únicamente cuando contiene el
   alias de **exactamente una** categoría. Dos posibles o ninguna → **queda sin categoría**, nunca
   `OTHER`: «Otros» es una categoría real del catálogo y usarla de tacho la convierte en mentira. El
   producto sin resolver se marca para completar y aparece en el filtro «Sin categoría».
4. **Backfill** — `scripts/backfill_product_category.py`, dry-run/`--apply`,
   `--tenant`/`--all-active`, auditado, que reporta **cobertura** (resueltos por mapeo / por
   inferencia / sin evidencia), no sólo cuántos tocó. Corre **antes** del backfill de SKU.

**Aceptación:** un producto creado desde una compra con categoría en la fila la conserva · un nombre
que matchea dos categorías no se infiere · el informe dice cuántos quedaron sin evidencia.

---

# F-O.3 · «Otros» dice por qué está cada fila — y agrupa

**Subió de prioridad con la medición del Paso 0** (ver F-O.4): es la fase que vuelve manejable una
bandeja de 2.282 pendientes, porque el 99,6% son dos hojas y agrupadas son dos líneas. Además del
motivo por fila, la pantalla necesita **agrupar por archivo × hoja/motivo** y ofrecer **descarte e
importación en bloque por grupo**.


El backend ya devuelve `context_label` (el motivo textual), `headers` y `uploaded_file_id`
(`api/v1/others.py:86-107`) y la pantalla no renderiza ninguno de los tres
(`otros/page.tsx:211-267`): con 2.282 registros en 46 páginas, la bandeja es inusable. Todos los
sitios de captura setean `context_label`, así que el dato existe para el 100% de las filas.

`GET /others/summary` agrupado por archivo × motivo (con el nombre del archivo resuelto desde
`uploaded_files.original_filename`), filtros por archivo y por motivo en `GET /others`, y en la
pantalla el motivo por fila más el archivo de origen. **Aceptación:** de 46 páginas a una lista de
motivos con su conteo.

---

# F-O.4 · Rediseñada por la compuerta (2026-08-14): no eran filas en blanco

**La hipótesis original se midió y se cayó.** El Paso 0 sobre ASTERIA (2.282 pendientes):

| Motivo | Filas |
|---|---|
| `Ganancias` | 1840 |
| `ganancias 2` | 433 |
| `LD 2026 — Movimientos ambiguos` | 4 |
| `Fila sin fecha reconocible` | 3 |
| `LD 2025 — Movimientos ambiguos` | 2 |

Vacías del todo: **314 (13%)** — no dominan. El **99,6%** son **dos hojas enteras volcadas fila por
fila**: ese `context_label` no es un motivo, es el NOMBRE de la hoja, escrito por el capture de
`ingestion_import_service.py:6444-6453`, que cuando no puede clasificar una hoja crea un pendiente
por cada fila. Y `ganancias 2` no es ni venta ni gasto ni producto: es una **liquidación de haberes
entre socios** (columnas `col_2`=nombre, `col_3`=0.5, y un `Período: Liquidación de Haberes`).

**Decisión del usuario sobre qué hacer con una hoja ilegible:** seguir guardando las filas —cero
riesgo de perder dato— y que la bandeja las **agrupe**. O sea que el trabajo pesado lo hace **F-O.3**,
que ya estaba planificada: agrupando por archivo × hoja, 2.282 filas son 5 grupos, y se descartan las
2.273 de «Ganancias» en una acción. Se descartan las alternativas de "un registro por hoja" (cambia
el modelo y obliga a migrar lo existente) y "no guardar nada" (rompe la promesa de que ninguna fila
se pierde en silencio).

**Lo que queda como F-O.4, ya secundario:**
- **a) Filas 100% vacías no se capturan.** Siguen siendo 314 reales. `rows_to_dicts`
  (`file_parsing.py:729-746`) no las descarta y `_capture_unclassified` (`:1120-1122`) saltea sólo si
  el dict **no tiene claves**, no si todos los valores están vacíos. Se corrige **en la captura, no
  en el parser**: los `row_index` alimentan las anclas de idempotencia y las decisiones de riesgo, y
  descartar aguas arriba los desplazaría. El descarte se cuenta (`counts["filas_en_blanco"]`).
- **b) Una fila de agregado no es un movimiento** (hallazgo de la muestra): se capturó
  `{"fecha": "Subtotal", "dinero_egreso": "18334679.59", …}`. Una fila cuya celda de fecha dice
  `Subtotal`/`Total` es un resumen de la planilla, y tratarla como operación suma dos veces.
- **c) Descarte en bloque por grupo** desde «Otros», que es lo que vuelve accionable el punto
  anterior sin 46 páginas de trabajo manual.

La limpieza por script (`dismiss_empty_unclassified.py`) queda **sólo para las 314 vacías**: las
2.273 de «Ganancias» las descarta el usuario desde la pantalla, porque son una decisión de negocio
("esta hoja no va"), no un defecto de captura.

---

# F-V · Lo que la pantalla ofrece tiene que existir

**V.1 — recorrer la tabla.** `Table.tsx:34` tiene `overflow-x-auto`, pero el contenedor no es
focuseable (las flechas del teclado no hacen nada) y los dos gradientes laterales (`:24-32`) están
**siempre** encendidos: decoran, no indican. La barra de 6px existe (`globals.css:110-121`) pero vive
al fondo de un contenedor de 50 filas, o sea fuera de pantalla. Primero **medir en el navegador**
dónde desborda de verdad (el contenedor o un ancestro); después, sobre `components/ui/Table.tsx` —que
es la base de todas las tablas—: contenedor focuseable (`tabIndex={0}`, `role="region"`,
`aria-label`), gradientes condicionados a la posición real del scroll, barra alcanzable y **columna
de acciones siempre visible**. Probado a la resolución de la captura (1440 CSS px), no sólo en
desktop ancho.

**V.2 — filtros honestos.** `GET /products/categories` devuelve el catálogo del vertical más las
custom del tenant (`api/v1/products.py:282-295`), no las categorías que los productos tienen; el
filtro compara contra el código (`products/page.tsx:288`). Se cuenta por categoría sobre los datos ya
cargados y se muestra el número (`Textiles (0)`) o se deshabilita la opción vacía, con empty state
explícito. En Gastos el conteo va sobre el período seleccionado y el mensaje tiene que decirlo
(`expenses/page.tsx:232-239`). **V.2 no sustituye a F-CAT**: vuelve honesto el filtro; quien
identifica las categorías es F-CAT.

---

# F-I · Identidad por código: comprobantes y wireo del resolvedor (recortada, ver F-ID)

**Lo que F-I ya no hace** (absorbido por F-ID, arriba): la migración de columna de código externo.
`entity_identifiers` la reemplaza — soporta VARIOS códigos por entidad con procedencia, no uno.

**Entregado (F-ID.7, 2026-08-15):** el motor F7b (`identity_resolution.py`) ganó un tier `"code"` de
máxima prioridad (por encima de documento) sin reescribir su lógica de match/conflicto, que ya era
genérica. Targets nuevos `customer_business_code`/`supplier_business_code` en
`GET /ingestion/field-catalog` (mismo patrón que F-S.0 con `sku`/`barcode` en venta). El índice de
referencia de fila resuelve por `vektor_code` propio de la entidad Y por cualquier `business_code`
ya registrado en `entity_identifiers` (bootstrap F-ID.4 o un import anterior) — nunca sólo el que
trae la fila actual. Cableado en los 4 call sites reales (2 rutas × cliente/proveedor). Verificado:
sin columna de código mapeada, cero cambio de comportamiento.

**Deuda declarada, documentada a propósito:**
1. No se captura el `business_code` de una fila MATCHEADA por otra vía (ej. documento) hacia
   `entity_identifiers` para que la PRÓXIMA importación lo reconozca sin bootstrap — habría tocado
   4 sitios más en caliente por un beneficio incremental; el bootstrap (F-ID.4) y el backfill
   (F-ID.6) ya cubren el caso principal.
2. **"Dos códigos iguales dentro del mismo archivo → 422, nunca last-wins" no se implementó** — es
   una regla de import MASIVO de MAESTROS (`customer_import_service.py`/`supplier_import_service.py`
   no tienen aún el concepto de `business_code`), no de la resolución de fila que sí se entregó.
3. El Nº de comprobante para agrupar líneas de una misma compra sigue reusando F-H6 sin cambios —
   nunca formó parte de este alcance.

Un código sigue siendo identidad **dentro de un tenant** — un archivo sin columna de código sigue
resolviendo por documento y nombre como siempre, F-I no vuelve obligatorio tener códigos.

**Aceptación (parcial — ver deuda arriba):** ✅ una venta/gasto con código resuelve al cliente/
proveedor correcto aunque el nombre venga escrito distinto · ✅ el código se ve en la ficha
(`vektor_code` en `CustomerResponse`/`SupplierResponse`, F-ID.5) · ⬜ re-importar el mismo archivo
no duplica maestros (depende del import masivo de maestros, fuera de este alcance) · borrar el
archivo revierte lo que creó (F11 sigue valiendo sobre las entidades nuevas, sin cambios acá).

---

# F-N · Nombre y apellido en una sola columna

**Parte sólo si la ficha es persona**: por la columna Tipo, o porque el documento es DNI y no
CUIT. Empresa o comercio → el nombre queda entero como razón social. Es lo correcto para
`Almacén Doña Rosa` y `Kiosco El Sol`, que partidos por el primer espacio darían el apellido
«Doña Rosa».

**Si no se puede saber, no parte y lo dice en pantalla.** Misma regla de no-invención que ya
gobierna las fechas (F6-A2), las filas sin monto (F-H4) y el envío sin comprobante (F-H6.b):
entre elegir mal y no elegir, Véktor conserva el dato y pregunta.

**Corte:** con coma, lo de antes es el apellido (`Pérez, Juan`); sin coma, primera palabra
nombre y el resto apellido, con las partículas (`de`, `del`, `de la`, `van`) pegadas al
apellido.

**Sin migración:** `last_name` ya existe en `customers` y `suppliers`, nullable, con el
docstring que dice que para empresas queda NULL. F-N usa lo que ya está.

**Aceptación:** una razón social nunca se parte · una persona con DNI sí · el caso indecidible
se ve en pantalla y no se resuelve solo · el split es visible antes de confirmar, no una sorpresa
en la ficha.

---

# F-D · Ruteo cross-sección controlado

Sintaxis `{entidad}:{campo}` **en inglés** (el español va en el label). El prefijo viaja dentro de `target_field`, así que `context_mappings` no cambia de forma: **sin migración**.

**La regla que gobierna la allowlist** (congelada en `test_ningun_cruzado_duplica_una_referencia_canonica`, F-0): una ruta cruzada existe para alcanzar campos que la entidad de la hoja **no puede expresar**. Si el campo ya tiene contraparte canónica en la hoja de origen —convención `{entidad}_{campo}`: `customer_dni`, `supplier_name`, `product_name`— queda **fuera**. Dos rutas para la misma columna con semánticas de creación distintas (la canónica pasa por el resolvedor de referencias, gobernado por `*_REFERENCE_CREATION_MODE`; la cruzada escribiría el maestro directo) es un bug esperando, sin nadie que arbitre cuál gana.

Esto corrige el borrador previo, que excluía `sale → product:name` por ese principio y a la vez habilitaba `sale → customer:name` y `expense → supplier:name`, que son el mismo caso.

**Permitido:** venta → campos de cliente **sin** contraparte canónica (`last_name`, `address`, `locality`, `province`, `postal_code`, `customer_type`, `iva_condition`) · gasto/compra → identificadores de producto (`sku`, `barcode`, `unit_cost_ars`, `category`) y campos de proveedor sin contraparte (`last_name`, `payment_method`).

**Prohibido:** `venta → product:stock_units` (doble guard: fuera de la allowlist **y** en `CROSS_ENTITY_FORBIDDEN_FIELDS`) · `sale → product:{name,sku,barcode}`, que son identidad y viven en el optgroup "Campos de esta venta" · **`product → supplier:*`**, que recrearía las filas marca-como-proveedor que cerró la Reforma de Proveedores y hubo que limpiar con `deactivate_brand_suppliers.py` + `_brand_collapsed` (si F-D la quiere, primero tiene que definir que sólo VINCULE a un proveedor existente y nunca cree) · venta → costos o precios vigentes por inferencia · el producto cartesiano entidad × campo.

**Semántica:** `fill_if_empty` — nunca pisa un dato existente por default (1187 ventas dan 1187 valores posibles del mismo campo; pisar hace ganar a la última fila del archivo, que es elegir un dato de negocio por un detalle de implementación). Agrupar por identidad, resolver conflictos **dentro del archivo antes de escribir**, **una escritura por entidad resuelta, no una por fila**. Preview: *"Tres filas proponen dos teléfonos distintos para el mismo cliente."*

**Procedencia (bloqueante para F11):** `_trae_maestros:1799` debe incluir los cross a `customer:`/`supplier:`. Sin eso, un archivo de ventas con `customer:email` modifica clientes y el DELETE responde `fully_reverted: true` **mintiendo**.

**Seis sub-commits:** contrato y allowlist → validación + doble guard de stock → resolución de identidad → buffer y `fill_if_empty` → preview y contadores → persistencia, trazabilidad y E2E.

---

# F-E · Simetría cliente/proveedor  (ADELANTADA el 2026-08-14)

**Por qué se adelantó:** F-I puede terminarse entera y la pantalla seguir mostrando «Local» y «No
identificado». Medido sobre el código: una venta **nunca** crea cliente
(`ingestion_import_service.py:3866-3881`) y un catálogo de productos **nunca** crea proveedor
(Reforma marca≠proveedor). Sin este contrato, ASTERIA no ve aparecer un solo proveedor por más
códigos externos que se agreguen.

**El contrato, que hoy vive repartido en tres lugares y hay que fijar en tabla y testear:**

| Hoja | Clientes | Proveedores |
|---|---|---|
| Maestra (clientes / proveedores) | crea y actualiza | crea y actualiza |
| Transacción (venta / compra) | **sólo vincula**; sin match → «Local» con traza | según el modo: `legacy` crea, `link_only` sólo vincula; sin dato → «No identificado» |
| Catálogo de productos | no aplica | **nunca**, ni crea ni vincula (la marca va a `custom_fields["marca"]`) |

**Los cuatro casos de una venta, medidos** (`_classify_row_reference:467-512`; las claves fuertes son
documento → email → teléfono, **el nombre nunca es clave**):

| La venta… | Hoy | Falta |
|---|---|---|
| trae nombre, sin código ni documento | `unresolved` → «Local»; el nombre queda en `custom_fields._customer_reference_raw` | una cola que lo muestre y permita crear en bloque |
| no tiene hoja maestra de clientes detrás | todo `anonymous`/`unresolved` → «Local» | **es ASTERIA hoy** |
| usa una variante del nombre de un cliente que existe | `unresolved` → «Local» **aunque el cliente exista** | sin documento ni código, la variante no matchea jamás — lo resuelve F-I |
| no trae el código que sí está en el maestro | cae a nombre → `unresolved` | el código tiene que viajar en la transacción, no sólo en el maestro |

**No se activa hasta cerrar la contradicción de configuración.** El default de código de `SUPPLIER_REFERENCE_CREATION_MODE` es `"legacy"` (**V11**); hay que confirmar contra Railway antes de asumir nada — es lo PRIMERO de la fase, no un pendiente lateral (cuarto intento).

- Agregar `CUSTOMER_REFERENCE_CREATION_MODE`, default `link_only`.
- **Cola de `unresolved`** con acción de crear en bloque desde ahí: es la salida que hoy no existe
  para el caso «trae nombre y no matchea», y sin ella «sólo vincula» significa «se pierde».
- Pasar proveedor a `link_only` sólo con rollout explícito y observable.
- En ambos casos: desde una transacción **se vincula**; no se crean maestros en silencio; la creación explícita ocurre desde su sección o flujo dedicado.
- Un nombre **no es identidad**: "Juan Perez", "juan perez", "Juan Pérez" y "J. Perez" crearían 4 clientes. Clave fuerte = documento válido (`validate_dni`/`validate_cuit`) | email | teléfono.

**Sentinelas — "Local" y "No identificado":** no se renombran, no se enriquecen con datos importados, no se fusionan, no se eliminan, **no se usan como prueba de identidad**. "Local" sigue siendo la vía del comprador al paso. Los dos tests de "Local" verifican **inmutabilidad e imposibilidad de merge/enrichment**.

## F-E.2 · El proveedor declarado desde el catálogo

**El caso que lo motiva, confirmado por el usuario (2026-08-14):** la columna `Tienda` del catálogo
de ASTERIA es **dónde compra cada producto** — el proveedor. Es el único lugar del archivo donde
están, y es justamente el lugar del que hoy está prohibido sacarlos: la Reforma de Proveedores cerró
`catálogo → proveedor` porque los catálogos venían creando un proveedor por cada MARCA, y hubo que
limpiarlo con `deactivate_brand_suppliers.py` y el flag `_brand_collapsed`.

**La distinción que habilita la excepción sin reabrir el desastre:** lo que creó las
marcas-como-proveedor fue la detección **automática por heurística de encabezado**. Una columna que
el usuario **mapea explícitamente** a proveedor es una declaración, no una adivinanza — y el
principio ya está escrito en F-0: *una sugerencia automática nunca equivale a una confirmación*.

- La ruta `product → supplier` se habilita **sólo por mapeo explícito**, nunca por heurística: ningún
  encabezado la sugiere solo. El prohibido de F-D sigue vigente para todo lo automático.
- Qué hace con el valor lo gobierna `SUPPLIER_REFERENCE_CREATION_MODE`, igual que una compra: en
  `link_only` vincula contra un proveedor existente y no crea; en `legacy` crea. **No se inventa un
  tercer comportamiento** para esta ruta.
- Una fila cuyo proveedor no resuelve va al centinela «No identificado» con traza, como cualquier
  otra referencia sin match — no se descarta ni se inventa.
- Test congelado: sin mapeo explícito del usuario, **ninguna** columna de un catálogo crea ni vincula
  proveedores, por más que se llame «Tienda», «Proveedor» o «Marca».

---

## Límites honestos de "cualquier archivo"

No es aceptar literalmente cualquier archivo, sino **cualquier estructura tabular dentro de los formatos soportados**: múltiples hojas, headers desconocidos, columnas extra, orden arbitrario, nombres propios del negocio, entidades mezcladas, filas parcialmente completas, catálogos/movimientos/resúmenes.

---

## Verificación

**Por etapa, antes de cerrarla:** `cd backend && make check` y la suite con el entorno del CI (`backend/.venv/bin/python -m pytest`, SQLite en memoria, `ENABLE_EMAIL_VERIFICATION=false`), corriendo **los mismos comandos que `ci-backend.yml`**. **Nunca `ruff format`/`make format`** durante una fase: el backend no está normalizado y reformatea el archivo entero (ver la regla en `CLAUDE.md`). `git diff --stat` + `git diff --check` antes de cada commit. Frontend: `npm run type-check && npm run lint && npm run test`. Todo test nuevo de regresión se **mutation-testea**: revertir el fix y confirmar que falla.

La compuerta de F-H3 es un test **HTTP end-to-end** con un `.xlsx` de tres hojas
generado en el propio test y parseado por el parser de producción
(`test_ingestion_replay_end_to_end.py`). Reemplaza al smoke sobre un tenant demo, que
se sacó: no representa el entorno real, no corre en CI y no prueba nada que este test
no pruebe con base aislada. El libro se arma con openpyxl en vez de versionar un
binario, para poder leer qué contiene sin abrirlo con Excel.

| Fase | Test compuerta |
|---|---|
| F-0 | dos columnas al mismo `custom_field:` → 422; `_resolve_target_cols` first-wins; `product:stock_units` desde venta rechazado |
| F-H1 | libro con Ventas antes que Productos: las ventas resuelven contra el catálogo del mismo archivo (**V1**+**V2**) |
| F-H1 | hoja sin columna de producto → importa como ingreso no inventariable |
| F-H1 | producto informado sin identidad → `/otros` con candidatos, no en `sales_entries` |
| **F-H2** ✅ | **venta 10/03 + compra 20/03 del mismo producto → vincula (identidad) pero `temporally_available = false`** |
| F-H2 ✅ | la venta vincula contra el producto que declara la hoja de compras, sin importar la solapa ni la fecha |
| F-H2 ✅ | compra ANTERIOR a la venta → sin advertencia (control: si no, la advertencia estaría prendida siempre) |
| F-H2 ✅ | producto preexistente en la base → fuera del chequeo, sus ventas viejas no se marcan |
| F-H2 ✅ | la huella de idempotencia numera la fila DENTRO DE SU HOJA (mutation-testeado: índice global → rojo) |
| F-H3.a ✅ | `historical_replay` no es el default de NINGUNA combinación de hoja (7 entidades × 7 mapeos) |
| F-H3.a ✅ | un efecto para una hoja inexistente → 422 con traza, antes del lease |
| F-H3.a ✅ | el efecto RESUELTO queda en el `STAGE_CONFIRM` (el default no viaja en el payload) |
| F-H3.b ✅ | apertura 10 + compra 5 − venta 4 → saldo final proyectado 11, **stock real sin tocar** |
| F-H3.b ✅ | a igual fecha, la compra entra antes que la venta (sin negativo intermedio falso) |
| F-H3.b ✅ | el catálogo declara un ABSOLUTO: pisa el saldo previo, no se le suma |
| F-H3.b ✅ | el saldo de apertura es el PREVIO al archivo (registrar tras `_apply_purchase_to_stock` lo contaría dos veces — mutation-testeado) |
| F-H3.b ✅ | venta 10/03 + compra 20/03 → `primer_negativo_en = 10/03`, saldo final 4: tocar negativo ≠ quedar negativo |
| F-H3.b ✅ | una hoja `no_inventory` no entra en la proyección |
| F-H3.c ✅ | el confirm devuelve el impacto por producto; el stock real no se movió |
| F-H3.c ✅ | con más productos que el máximo listado, `inventory_impact_total` reporta el TOTAL, no lo listado (mutation-testeado) |
| F-H3.d.1 | una venta importada que NO descontó no cuenta en `stock_esperado` (hoy da divergencia falsa) |
| F-H3.d.1 | una venta EN VIVO sin su movimiento **sigue** dando divergencia (control: si no, el chequeo dejó de detectar) |
| F-H3.d.2 | la venta importada guarda su `context_id`; una venta manual no gana la clave |
| F-H3.d.3 ✅ | `historical_replay` + stock insuficiente: la fila NO entra a `sales_entries` y aparece en `/otros` con el motivo |
| F-H3.d.3 ✅ | el rechazo se decide por FECHA, no por orden de solapa (mutation-testeado: orden del Excel → rojo) |
| F-H3.d.3 ✅ | `informational` (default) con el mismo archivo: importa TODO, nada a `/otros` |
| F-H3.d.3 ✅ | el gate corre para el ARCHIVO, no por hoja: dos hojas de ventas comparten el mismo stock (mutation-testeado) |
| F-H3.d.3 ✅ | una fila rechazada NO consume stock: no arrastra a las que sí entraban |
| F-H3.d.3 ✅ | re-confirmar no duplica la captura en `/otros` (mutation-testeado: sin huella → dos filas) |
| F-H3.d.3 ✅ | el archivo plano gatea igual; si además crea productos, ver las filas de F-F.1 |
| F-H3.d.4 ✅ | aplicar dos veces no descuenta dos veces (`source_event_id="sale:{id}"`, mutation-testeado) |
| F-H3.d.4 ✅ | una venta ya descontada EN VIVO no se vuelve a descontar acá (**V13**) |
| F-H3.d.4 ✅ | el impacto que devuelve el apply se recalcula contra el stock actual, no el del confirm |
| F-H3.d.4 ✅ | si el stock cambió entre confirm y apply, el descuento queda **pendiente**: no se anula la venta (mutation-testeado) |
| F-H3.d.4 ✅ | cargar el stock que faltaba y reintentar sí lo aplica |
| F-H3.d.4 ✅ | borrar el archivo revierte los movimientos del replay — **end-to-end**, no por la columna (mutation-testeado) |
| F-H3.d.4 ✅ | `dry_run` calcula y no escribe; sólo aplica las hojas pedidas; una venta sin hoja registrada lo **declara** |
| F-H3.d.5 ✅ | el panel sin `fileId` es sólo informativo; con sólo compras no ofrece aplicar (no hay nada que hacer) |
| F-H3.d.5 ✅ | tras aplicar muestra lo que devolvió el SERVIDOR, y dice que las ventas sin stock **no se anularon** |
| ~~F-H3.d.6~~ | **REVERTIDO POR F-F.1.** El rechazo pre-lease del archivo plano (422 `replay_no_gateable`), su `STAGE_REJECT` y la degradación a `informational` del importador (`replay_degradado`) se eliminaron: existían porque el gate miraba un saldo estático, y las compras del archivo ahora entran como créditos datados. Las filas de abajo son las que las reemplazan. |
| F-H3.d.6 ✅ | archivo de UNA tabla con mapeo por contexto: `quantity` llega al importador (antes se perdía y toda venta valía 1 unidad) |
| F-F.1 ✅ | ningún archivo se rechaza por ser plano: el que declara stock y ventas juntas se confirma, y no deja `STAGE_REJECT` (control por el otro lado) |
| F-F.1 ✅ | la compra del archivo del **01/03** respalda la venta del 10/03; con las fechas invertidas (compra 20/03) la misma venta se va a «Otros» (mutation-testeado: créditos sin fecha → rojo) |
| F-F.1 ✅ | a igual fecha el crédito entra antes que el débito, mismo desempate que `replay_timeline` |
| F-F.1 ✅ | el saldo de partida es el PREVIO al archivo, no el de hoy: pasar un saldo que ya incluye las compras **y** los créditos las contaría dos veces |
| F-F.1 ✅ | que el archivo también cargue productos ya no apaga el gate ni degrada la hoja (`hojas_con_replay=1`) |
| F-F.2 ✅ | producto en cero, sin movimientos vivos y que el archivo no declara: la venta **entra**, su descuento queda pendiente y el confirm lo avisa (mutation-testeado: "todo saldo es conocido" → rojo) |
| F-F.2 ✅ | control con el mismo archivo y 2 unidades cargadas: la venta de 6 sí se va a «Otros» y NO se avisa de pendiente (si no, la regla estaría apagando el gate entero) |
| F-F.2 ✅ | cuenta como conocido: saldo previo > 0, lo que el archivo declara o compra, o cualquier movimiento vivo en el ledger |
| F-F.3 ✅ | el confirm de una hoja `historical_replay` deja el stock ya descontado (no hace falta un segundo clic) y lo avisa |
| F-F.3 ✅ | borrar el archivo devuelve las unidades y responde `fully_reverted: true`: el movimiento del confirm lleva `source_upload_id` y el guard del ledger no se prende por el `updated_at` que mueve el descuento |
| F-F.3 ✅ | tocar «aplicar» en el panel después de ese confirm es un no-op (`ya_aplicadas`), no un segundo descuento: los dos caminos comparten `sale:{id}` |
| F-F.3 ✅ | la hoja `informational` sigue sin tocar una unidad, y la etapa `replay_inventario` ni siquiera aparece en la traza |
| F-F.3 ✅ | el descuento se aplica también con la sesión de producción (`autoflush=False`), que la del conftest no reproduce |
| F-F.3 ✅ | el desglose F-T declara `replay_inventario` con su denominador de filas |
| F-F.3.b ✅ | 100 ventas de 10 productos: 671 → 87 sentencias en el confirm y 100 → 1 envío al broker, con el MISMO instrumento en las dos corridas |
| F-F.3.b ✅ | varias ventas del mismo producto dejan el saldo acumulado y **un movimiento por venta** (el lote ahorra viajes, no traza) |
| F-F.3.b ✅ | el clamp corre paso a paso (mutation-testeado: clamp al final → rojo) y `current_qty` sigue pudiendo ser negativo |
| F-F.3.b ✅ | la carrera con una venta en vivo rehace el lote de a una: las otras entran, la conflictiva cuenta como ya aplicada y NO se crea un segundo movimiento (mutation-testeado: sin fallback → rojo) |
| F-F.3.b ✅ | un `STOCK_DECREASED` por corrida y una `STOCK_ALERT_CREATED` por producto bajo umbral, no una por venta |
| F-H3.d | idempotencia del import intacta tras pasar a dos pasadas |
| **F-H3** ✅ | **`historical_replay`: apertura 10 + compra 5 − venta 4 → `stock_units` final = 11, cruzando `.xlsx` real → confirm → apply → saldo persistido** |
| F-H3 ✅ | re-confirmar el mismo archivo no aplica el movimiento dos veces |
| F-H3 ✅ | borrar el import revierte exactamente sus movimientos (incremental, nunca `Σ(movimientos)`) |
| F-H3 ✅ | `informational` no descuenta la venta (la apertura y la compra sí tocaron el stock: el eje es de la HOJA) |
| F-H3.e ✅ | el endpoint propone un modo por hoja y sólo ofrece los que tienen sentido para esa hoja |
| F-H3.e ✅ | sacar `cantidad` del mapeo deja a la hoja sin poder aplicar su historia (el default se recalcula con el borrador) |
| F-H3.e ✅ | el modo elegido VIAJA en el confirm — argumento del componente y cuerpo del POST, mutation-testeado por capa |
| F-H3.e ✅ | el modo que ofrece el selector CAMBIA lo que hace el confirm, desde el payload que manda la pantalla y no sólo desde un curl (antes se verificaba contra el 422 de d.6; ahora contra el gate corriendo) |
| F-H3 | `current_snapshot` fija absoluto; `no_inventory` no crea movimiento |
| F-H3 | replay histórico negativo → advertencia, **no** `InsufficientStockError` |
| **F-H4** ✅ | **las 7 filas de la tabla de precio, incluida la discrepancia con tolerancia de 1 centavo** |
| F-H4 ✅ | confirm HTTP sin columna de monto, con `unit_price`+`quantity` → 200 y la venta queda con el monto calculado (hoy daba 422: sin esto la derivación es inalcanzable desde la pantalla) |
| F-H4 ✅ | media alternativa (sólo precio, o sólo cantidad) → sigue pidiendo el monto, con traza `requeridos_sin_mapear` |
| F-H4 ✅ | `custom_field:amount` no cubre el requerido, y `custom_field:unit_price`+`custom_field:quantity` tampoco cubren por alternativa |
| F-H4 ✅ | el camino plano y el multi-hoja dan la MISMA venta sobre las mismas filas (mutation-testeado: la compuerta `wants_ventas` vieja → rojo) |
| F-H4 ✅ | cantidad vacía con precio mapeado NO vale `precio × 1` (mutation-testeado: usar `_venta_cantidad` con su piso → rojo) |
| F-H4 ✅ | la fila que no se puede resolver va a "Otros" con el motivo; la fila de relleno (todas las celdas vacías) NO (mutation-testeado) |
| F-H4 ✅ | eliminar la columna de monto es legal si quedan precio y cantidad; con media alternativa sigue siendo violación (armonía con F8) |
| **F-H6.b** ✅ | **`Envío = 2.000` repetido en 10 filas del mismo comprobante → se cuenta UNA vez, no $20.000** |
| F-H6.a ✅ | la heurística NO conoce `precio_unitario`: sin el target explícito el costo de la compra se pierde (medido) |
| F-H6.a ✅ | el mapeo explícito de `unit_price` gana sin guardas; sin mapeo, la heurística sigue |
| F-H6.a ✅ | una línea de compra con precio y cantidad no necesita el total (F-H4 en compras) |
| F-H6.b ✅ | sin comprobante y sin decisión: no se cobra nada y se reporta |
| F-H6.b ✅ | «un solo envío» colapsa la repetición y cobra una vez por cifra; «cada fila» no colapsa nada |
| F-H6.b ✅ | re-confirmar no cobra el flete dos veces, tampoco con «cada fila es un envío» |
| F-H6.b ✅ | una decisión sobre una hoja sin columna de envío → 422 antes del lease |
| F-H6 | distribución por subtotal cuadra al centavo; no distribuido no toca `unit_cost_ars`; el gasto atribuido a inventario no se cuenta dos veces |
| F-T | el confirm publica su desglose por etapa en `pipeline_events`, con el mismo archivo antes y después |
| F-F | dos libros con las mismas ventas y las solapas invertidas dan el mismo stock final |
| F-F | un archivo plano con stock y ventas juntos **importa**: ya no se rechaza pre-lease |
| F-F | una hoja sin producto+cantidad no renderiza ninguna pregunta ni cartel de inventario |
| F-F | el ancla del catálogo se aplica antes de todos los eventos: el caso don pedro no descuenta dos veces |
| F-R | releer el mismo archivo sin cambios da correspondencia 1:1: cero pérdida neta, cero duplicados |
| F-R | una hoja que cambia de clasificación **no puede aplicarse** sin aceptación explícita (`sin_reemplazo > 0`) |
| F-R | los conteos por entidad (ventas/gastos/productos/clientes/proveedores) cuadran antes y después |
| F-S.0 | catálogo y ventas en la MISMA carga vinculan por el código que trae el archivo |
| F-S.0 | vincular un nombre a mano una vez resuelve todas las ventas que lo repiten (alias persistido) |
| F-S.0 | una venta sin producto resoluble queda contada y visible, nunca linkeada por adivinanza |
| F-ID | ningún código traído por el negocio se pisa; cero códigos repetidos por tenant, nunca reciclados |
| F-ID | el backfill corrido dos veces no cambia nada; una entidad nueva por cualquier vía nace con código |
| F-ID | agregar una categoría al catálogo sin prefijo de SKU **rompe el CI** (no cae a `GEN` en silencio) |
| F-ID | dos identificadores fuertes contradictorios en la misma fila dan `conflict`, nunca gana el primero |
| F-ID | fusionar dos entidades transfiere sus identificadores al sobreviviente, nunca los pierde |
| F-CAT | un nombre que matchea dos categorías **no** se infiere; el informe dice cuántos quedaron sin evidencia |
| F-CAT | un producto creado desde una línea de compra con categoría en la fila la conserva |
| F-O.3 | cada fila de «Otros» dice su motivo y su archivo; el resumen agrupa 46 páginas en una lista de motivos |
| F-O.4 | una planilla con filas en blanco al final no genera pendientes, y el descarte se cuenta |
| F-V | la tabla se recorre con teclado y la columna de acciones no queda tapada a 1440 CSS px |
| F-V | el filtro no ofrece una categoría que ningún producto tiene sin decir que está vacía |
| F-E | una venta con nombre que no matchea deja el dato visible en una cola, no sólo en «Local» |
| F-I | re-importar el mismo archivo no duplica maestros (el código externo matchea) |
| F-I | una venta con `CLI-01` resuelve al cliente aunque su nombre esté escrito de tres formas distintas |
| F-I | dos filas con el mismo código en el mismo archivo → 422, nunca last-wins |
| F-N | `Almacén Doña Rosa` no se parte; `Pérez, Juan` sí; el indecidible se ve en pantalla |
| F-A | una hoja cuya única columna candidata a fecha se auto-propone como custom **sigue** reportando `required_missing` (**V10**) |
| F-A | cambiar la sección preserva lo tocado |
| F-B | no aparece ningún `%` en el DOM del panel |
| F-D | borrar un archivo revierte un cliente **modificado** por columna cruzada (F11) |
| F-E | "Local" inmutable: no se renombra, no se enriquece, no se fusiona — con el flag prendido |

**Smoke end-to-end** sobre un **tenant demo** (nunca una cuenta real): `.xlsx` con Ventas primero y Productos después, una compra con envío repetido en varias líneas del mismo comprobante, una venta anterior a su única compra, y una venta cuyo producto no existe. Confirmar que las ventas vinculan, que el stock final refleja el replay, que el envío se cuenta una vez, que la fila huérfana cae en `/otros` y que el resumen lista las incidencias con su severidad.

---

## Pendiente del usuario

- **Confirmar contra Railway** el valor real de `SUPPLIER_REFERENCE_CREATION_MODE` (**V11**) — condiciona el default de F-E, que ahora es una fase adelantada y no la última.
- **Correr el Paso 0** contra Neon (el usuario provee `DATABASE_URL` desde su shell): es compuerta de la limpieza de F-O.4 y de los backfills de F-S y F-CAT.
- **Decidir `has_catalogo_fuerte`.** En `file_parsing.py::infer_spreadsheet_type`, `articulo`/`producto` sin tilde se tratan como señal inequívoca de catálogo y cortan con `return "stock"` antes de mirar cualquier señal de venta: una planilla con `Fecha + Cliente + Facturación + Método de pago` se clasifica como catálogo por nombrar su columna «Artículo». **Ya está en producción** — el fix de tildes de PR #47 sacó la excepción que la tilde le daba por accidente— y es exactamente el escenario que F-R tiene que bloquear. Dos caminos: dejarlo como deuda declarada, o que `has_catalogo_fuerte` no gane cuando hay contexto de operación fuerte (recomendado). Toca el clasificador central del que dependen las reglas de maestros (F7a) y el libro de compras, para TODOS los tenants — no se decide solo.
- Persistir este plan a `docs/plans/` una vez aprobado, para que no dependa de la sesión.
