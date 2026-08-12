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
F-T  medir el confirm antes de agregarle trabajo
F-F  fechas mandan: todo movimiento afecta el inventario
F-A  nombre original + preservación de edición
F-B  claridad visual + extracción del monolito
F-I  identidad por código: IDs y comprobantes
F-N  nombre y apellido en una sola columna
F-D  ruteo cross-sección
F-E  simetría cliente/proveedor (paralelo desde F-0; no se activa hasta cerrar defaults)
F-H6.f el camino plano cobra el envío y honra las decisiones de costo
```

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

**La excepción es F-I y es deliberada:** un código externo (`CLI-01`, `PROV-03`) es identidad, y la identidad no puede vivir en `custom_fields` — necesita un índice único por tenant para que re-importar el mismo archivo no duplique maestros, y `custom_fields` es JSONB sin restricción de unicidad. Migración aditiva: columna `external_code` en `customers`, `suppliers` y `products` (detalle en F-I). Se declara acá para que la promesa de "sin migraciones" no se lea como vigente cuando ya no lo es.

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

**Aceptación:** ningún archivo se rechaza por ser plano · el orden de aplicación es por fecha y
no por solapa (dos libros con las mismas ventas y las solapas invertidas dan el mismo
resultado) · una hoja sin producto+cantidad no muestra una sola línea sobre inventario · el
caso don pedro sigue en rojo si se rompe el anclaje.

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

# F-I · Identidad por código: IDs y comprobantes

**El síntoma que la motiva.** En el archivo real, la columna `ID` de Proveedores y de Clientes
termina en `custom_field:id_proveedor` con el cartel «esta hoja no tiene un campo para eso
(codigo)». Véktor entiende el concepto y no tiene dónde ponerlo. Mientras tanto, «Almacén Doña
Rosa», «Almacen Doña Rosa» y «ALMACEN D ROSA» —las tres variantes que el propio archivo declara
en la columna *Variantes de nombre vistas en ventas*— son tres clientes distintos.

**Migración aditiva** (la excepción declarada arriba): columna `external_code VARCHAR(64) NULL`
en `customers`, `suppliers` y `products`, con índice único parcial por `(tenant_id,
external_code)` donde no es null. Verificado: hoy no existe ninguna columna equivalente en los
tres modelos.

**Jerarquía del resolvedor de maestros:** `código externo → documento/CUIT → nombre
normalizado`. Un código siempre le gana a un nombre parecido. Espeja lo que `_resolve_product`
ya hace con `barcode → sku → nombre+marca` (F2): la regla no es nueva, faltaba la clave.

**Vínculo entre hojas**, que es lo que resuelve el archivo real: una venta cuya columna Cliente
trae `CLI-01` encuentra al cliente que la hoja Clientes declaró con ese ID. El orden
maestro→transacción ya existe (F7c); lo que falta es la clave por la cual buscar. Lo mismo con
el Nº de comprobante: las líneas que lo comparten son **una** compra — el agrupamiento ya existe
en F-H6 y se reusa, no se reescribe.

**Targets nuevos** en `GET /ingestion/field-catalog` para las tres entidades, más el target de
referencia cruzada del lado de la transacción. Sin eso la columna sigue cayendo a campo propio.

**Dos códigos iguales dentro del mismo archivo → 422 legible**, nunca last-wins. Misma regla que
`SINGLE_VALUE_FIELDS`: si el archivo se contradice, lo dice, no elige por orden de fila.

**Límite honesto:** un código es identidad **dentro de un tenant**. `CLI-01` de dos negocios
distintos son dos clientes distintos, y por eso el índice lleva `tenant_id`. Un archivo sin
columna de ID sigue resolviendo por documento y nombre como hoy — F-I no vuelve obligatorio
tener códigos.

**Aceptación:** re-importar el mismo archivo no duplica maestros · una venta con código resuelve
al cliente correcto aunque el nombre venga escrito distinto · el código se ve en la ficha ·
borrar el archivo revierte lo que creó (F11 sigue valiendo sobre las entidades nuevas).

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

# F-E · Simetría cliente/proveedor

**No se activa hasta cerrar la contradicción de configuración.** El default de código de `SUPPLIER_REFERENCE_CREATION_MODE` es `"legacy"` (**V11**); hay que confirmar contra Railway antes de asumir nada.

- Agregar `CUSTOMER_REFERENCE_CREATION_MODE`, default `link_only`.
- Pasar proveedor a `link_only` sólo con rollout explícito y observable.
- En ambos casos: desde una transacción **se vincula**; no se crean maestros en silencio; la creación explícita ocurre desde su sección o flujo dedicado.
- Un nombre **no es identidad**: "Juan Perez", "juan perez", "Juan Pérez" y "J. Perez" crearían 4 clientes. Clave fuerte = documento válido (`validate_dni`/`validate_cuit`) | email | teléfono.

**Sentinelas — "Local" y "No identificado":** no se renombran, no se enriquecen con datos importados, no se fusionan, no se eliminan, **no se usan como prueba de identidad**. "Local" sigue siendo la vía del comprador al paso. Los dos tests de "Local" verifican **inmutabilidad e imposibilidad de merge/enrichment**.

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

- **Confirmar contra Railway** el valor real de `SUPPLIER_REFERENCE_CREATION_MODE` (**V11**) — condiciona el default de F-E.
- Persistir este plan a `docs/plans/` una vez aprobado, para que no dependa de la sesión.
