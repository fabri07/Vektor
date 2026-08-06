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
| **V12** | Ya existe `stock_treatment` **por hoja** (`{context_id: "opening_balance"\|"purchase"}`). Es el control donde debe vivir el efecto de inventario, no un selector nuevo. | F10 |

**Conclusión de V1+V2:** la jerarquía no falla por falta de regla, falla por **orden y visibilidad**. Ordenar por entidad (product → expense → sale) resuelve la **identidad** —y sólo eso: no convierte a la compra del 20/03 en justificación de la venta del 10/03, porque eso se decide comparando fechas, no por el orden en que se aplicó (F-H2). El orden **cronológico** es otra cosa y sirve para otra: reproducir cuántas unidades había, que recién importa cuando las ventas mueven stock (F-H3.0).

---

## Orden de entrega

```
F-0  contrato e invariantes (sin cambio de comportamiento)          ✅ entregado
F-H1 jerarquía: la identidad existe antes de que alguien la busque  ✅ entregado
F-H2 identidad ≠ validez temporal (la evidencia se juzga por fecha) ✅ entregado
F-H3 efecto de inventario por hoja + cola cronológica  ← corrige datos
F-H4 precio unitario × cantidad = monto
F-H6 costos de compra agrupados (envío, costo final)
F-A  nombre original + preservación de edición
F-B  claridad visual + extracción del monolito
F-C  obligatorios explicados
F-D  ruteo cross-sección
F-E  simetría cliente/proveedor (paralelo desde F-0; no se activa hasta cerrar defaults)
```

### Alcance de migraciones — declaración explícita

**El programa F-0 → F-E es aditivo y sin migraciones.** Todo viaja en columnas existentes (`target_field` es `String`, `inventory_movements` ya tiene `qty`/`unit_cost`/`source_type`), en el payload del confirm o en `custom_fields`.

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

### F-H3.0 · La cola cronológica (movida acá desde F-H2)

Es el primer paso de la fase, porque **es acá donde el orden pasa a mover stock**. Dos pasadas:

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

Cada hoja declara qué significan sus cantidades. Extiende el `stock_treatment` por hoja que ya existe (**V12**) — no es un control nuevo:

| modo | comportamiento | equivalencia F10 |
|---|---|---|
| `historical_replay` | compras suman y ventas restan; **el stock final refleja las ventas** | ≈ `purchase` |
| `current_snapshot` | el archivo declara el saldo absoluto final | ≈ `opening_balance` |
| `informational` | calcula y advierte, **sin modificar stock** | — |
| `no_inventory` | cantidad puramente transaccional, no asociada a producto | — |

Los valores viejos (`opening_balance`, `purchase`) siguen aceptándose y se mapean al modo equivalente (retrocompatibilidad).

**Pregunta al usuario** cuando una hoja tiene producto y cantidad: *"¿Cómo deben afectar estas filas al inventario?"*, con default por tipo detectado:

| tipo detectado | default |
|---|---|
| Catálogo | `current_snapshot` |
| Compras | `historical_replay` |
| Ventas históricas | `historical_replay`, **con preview del resultado** |
| Resumen contable / libro diario | `no_inventory` |

**Esto corrige V3**: bajo `historical_replay` las ventas importadas **sí** generan movimiento `sale` y descuentan stock. Requisitos que lo hacen seguro:

- **Preview obligatorio del stock resultante** antes de confirmar (por producto: saldo previo → movimientos → saldo final).
- **Idempotencia por archivo, hoja y fila**: el movimiento lleva `source_upload_id` + `source_row_hash` (columnas existentes, **V4**). Nunca se aplica dos veces el mismo movimiento.
- **Eliminar el import revierte exactamente sus movimientos**, vía `void_movement` con ajuste incremental. `stock_units` **NUNCA** por `setattr` ni recalculado como `Σ(movimientos)` — su reversa es exclusivamente incremental (invariante ya pagado con un incidente).
- **Sin doble conteo con la venta en vivo**: `decrement_for_sale` sigue excluyendo las ventas con `source_upload_id`. Bajo `historical_replay` el import es el dueño del ledger de esa fila.
- **Sin doble conteo con el chequeo de integridad**: `inventory_integrity_service` ya ignora los `sale` del ledger; los movimientos nuevos llevan `source_type` propio (`sale_import`) para que la clasificación de `inventory_movement_origin.classify_stock_movement` los reconozca.
- **Stock negativo histórico NO levanta `InsufficientStockError`.** La prohibición de negativo es para ventas en vivo. Un replay histórico puede dar negativo legítimamente (falta el inventario inicial): **advierte, no bloquea**.

**Taxonomía de incidencias, cada una con su severidad:**

| código | severidad | efecto |
|---|---|---|
| `producto_no_resuelto` | **bloqueante de la fila** | → /otros con candidatos |
| `cantidad_cero_o_negativa` | **bloqueante de la fila** | → /otros |
| `stock_historico_negativo` | advertencia | importa; se reporta |
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

## F-H5 · Confirmación atómica

**Antes de escribir:** resolver identidades → construir la cola cronológica de movimientos en memoria → validar relaciones bloqueantes → calcular importables / rechazadas / warnings → **preview final** (incluye el stock resultante de F-H3).

**Al confirmar:** insertar sólo filas válidas · trazabilidad de las rechazadas · buffers por identidad (no una query por fila) · idempotencia por `(archivo, contexto, fila)` intacta.

`_add_sale`/`_add_expense` pasan de `bool` a `RowOutcome(inserted, product_id, customer_id, supplier_id)`. **Riesgo alto:** `_did_insert` alimenta `_register_import_row_fingerprint`. Test de idempotencia **antes** de tocar la firma.

## F-H6 · Costos de compra agrupados

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

**Archivos F-H:** `ingestion_import_service.py` (dos pasadas en `_insert_multisheet_data`, `_add_product` + índices, `_add_sale:4172`, `_add_expense:4270`, `_apply_purchase_to_stock:1944`, `RowOutcome`), `inventory_temporal_service.py` (reuso de `replay_timeline`), `inventory_movement_origin.py` (`sale_import`), `stock_service.py` (reversa incremental), `column_mapping_service.py` (targets de expense), módulo nuevo de aritmética de precios/costos, `schemas/ingestion.py` (modo de inventario por hoja + warnings estructurados).

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
| F-H3.0 | a igual fecha, la compra se aplica antes que la venta |
| F-H3.0 | idempotencia del import intacta tras pasar a dos pasadas |
| **F-H3** | **`historical_replay`: apertura 10 + compra 5 − venta 4 → `stock_units` final = 11** |
| F-H3 | re-confirmar el mismo archivo no aplica el movimiento dos veces |
| F-H3 | borrar el import revierte exactamente sus movimientos (incremental, nunca `Σ(movimientos)`) |
| F-H3 | `informational` no modifica stock; `current_snapshot` fija absoluto; `no_inventory` no crea movimiento |
| F-H3 | replay histórico negativo → advertencia, **no** `InsufficientStockError` |
| F-H4 | las 7 filas de la tabla de precio, incluida la discrepancia con tolerancia de 1 centavo |
| **F-H6** | **`Envío = 2.000` repetido en 10 filas del mismo comprobante → se cuenta UNA vez, no $20.000** |
| F-H6 | sin identidad de comprobante → no distribuye, queda gasto separado |
| F-H6 | distribución por subtotal cuadra al centavo; no distribuido no toca `unit_cost_ars`; el gasto atribuido a inventario no se cuenta dos veces |
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
