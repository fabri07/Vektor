# Mejoras de datos · Ruta B — `compra_catalogo_sin_costo` (3 PRs, sin migración)

Estado: **aprobado 2026-11-23**. B1 (refactor) en `main` (#269); B2 (mejora + vista previa) en curso; B3 (frontend) sigue. Continúa `compra_sin_costo` (ruta A, #268).

## Context
El mecanismo de «mejoras de datos» y la mejora `compra_sin_costo` (ruta A) ya están en `main` (#266, #267, #268). Quedan compras de stock de catálogo que el dueño cargó como «La compré ahora» y que quedaron **sin costo y sin gasto**: Camisa lino beige M (8 u.) y Pantalón cargo verde T42 (15 u.), movimientos `purchase` + `catalog_initial_stock`, fechados el 14-nov (fecha de carga), 0 gastos vivos, sin proveedor.

**Por qué quedaron así** (verificado): el importador de catálogo ignora una columna «costo total» para el costo unitario (`_resolve_unit_cost_col`: «NUNCA aceptar una columna de costo total») y, sin costo, `_add_catalog_initial_stock_cogs` retorna sin crear el gasto. El mapeo del confirm de esas hojas es `Producto→name, Cantidad→stock_units, «Costo total»→purchase_base_cost`, `stock_treatment="purchase"`. El total **no está en la base** (`products.custom_fields['purchase_base_cost']` vacío en ambos): sólo está en el xlsx original en S3.

**Principio del dueño:** «todo tiene que ver con todo». Cargar sólo el costo deja el libro a medias. Una compra con costo es: costo del movimiento + costo vigente del producto + ventas posteriores costeadas + **gasto COGS** + baja de banco/caja + resultado/margen/score. La mejora cubre toda la cadena, con Deshacer.

## Decisiones del dueño (cerradas)
1. Se hace la **opción B**: el sistema relee el xlsx original (no lo escribe el dueño).
2. El gasto se crea **aunque ya exista arqueo**. Se avisa en la vista previa y se deja en la auditoría que el dueño lo autorizó.
3. **Monto del gasto = el «Costo total» del archivo**, exacto. El costo unitario queda redondeado a centavos (`unitario × cantidad` puede diferir del total por centavos; es lo esperado).
4. **Medio de pago = el que dice la fila del archivo** («método de pago», «forma de pago», «pago»…). Se reusa la lectura del importador (`_val(row, cols.get("payment_method"), _PAGO_COLS)` + `cash_service.normalize_payment_method`). Sin columna o celda vacía → `transfer` (default del propio importador, `ingestion_import_service.py:10678`), y la vista previa lo dice. Valor con texto que normaliza a `other` o a `account` (cta cte, exigiría proveedor y no hay) → **a mano** con el motivo, sin adivinar.
5. **Fecha del gasto = fecha del movimiento** (14-nov): el archivo no mapea ninguna columna de fecha, así que no hay otra evidencia. Si el xlsx real trae una columna de fecha sin mapear, la vista previa la muestra como dato informativo; usarla es un PR aparte (cambiaría la fecha del movimiento y el recosteo de ventas).

## Diseño

### Clase y contrato
`compra_catalogo_sin_costo`, clase **`cambia_valores`** (nunca corre sola; vista previa + PIN). No hace falta flag nuevo: el mecanismo ya trata así a esa clase.

**Cambio chico al contrato (B2):** una mejora puede declarar `necesita_archivo = True` y exponer `evidencia(session, tenant_id, candidatos, s3) -> dict[id, Evidencia | MotivoANoCargar]`. El **servicio la llama ANTES de tomar el lock** (lectura de S3 sin bloquear el POS) y le pasa el resultado a `aplicar`, que **revalida el estado de la base bajo lock** (movimiento todavía sin costo, sin gasto vivo, producto sin ediciones). La evidencia del archivo es inmutable; lo que cambia es la base, y eso se re-chequea.

### Etapa 1 — SQL barato (la usa el panel; nunca toca S3)
Candidato = movimiento vivo `purchase` + `catalog_initial_stock`, `unit_cost` y `unit_cost_final` NULL, `qty > 0`, con `source_upload_id` y `source_row_ref` (≤ 64), **sin** gasto vivo del mismo `(tenant, upload, row, product)`, y cuyo **confirm más reciente del archivo** (`pipeline_events`, `stage='confirm'`) dice `stock_treatment == "purchase"` (string, o dict con alguna hoja en `purchase`) y tiene una columna de costo total mapeada (`_es_costo_total` de `compra_sin_costo.py`). Sólo cuenta «posibles».

### Etapa 2 — lee S3 (sólo vista previa y aplicar; una descarga por archivo)
- `reread_service._fresh_summary(file, s3)` re-descarga y re-parsea.
- **Ubicar la fila:** para cada contexto candidato (claves de `mappings.context` del confirm, más `None` para el camino plano) se enumeran las filas del bucket del contexto (`r["__context__"] == ctx`, mismo criterio que `_filas_y_mapeo`, `ingestion_import_service.py:11879`) y se recomputa `_source_row_ref(_import_row_anchor(tenant, file, ctx, i))` (`:2015`, `:2175`) hasta igualar el `source_row_ref` del movimiento.
- **Doble chequeo, sin confiar sólo en el hash:** el nombre de la fila (normalizado) = nombre del producto, y su cantidad (en unidad base, con `base_units_per_sale_unit`) = `qty` del movimiento. Si 0 o >1 coincidencias, o algo no calza → «a mano» con el motivo.
- Total: `_parse_amount` de la columna `purchase_base_cost` (o alias `_PRECIO_COMPRA_BASE_COLS`), **exigiendo** que el encabezado sea de costo total; `total > 0`. Costo unitario = `_costo_unitario_desde_total(total, qty)` (a 2 decimales, `ROUND_HALF_UP`, nunca 0).
- Tratamiento por hoja: con dict, el de la hoja donde se ubicó la fila (`_resolve_stock_treatment`, `:5615`).
- Motivos «a mano»: archivo borrado de S3, fila no ubicada / ambigua, nombre o cantidad distintos, sin total o total inválido, encabezado que no es de total, medio de pago no interpretable, tratamiento «apertura».

### Aplicar (bajo lock exclusivo + `lock_timeout`, tandas por archivo)
1. Movimiento: `unit_cost` + `unit_cost_final` con `contrato_declarado(costo, envio='desconocido')` → «estimado»; `recalcular_costo_vigente`.
2. **Ventas `sin_costo` que pasa a respaldar:** recosteo explícito antes del commit y `DataRepairItem(action='UPDATE_SALE', before/after)` (lógica de `CompraSinCosto._costear_ventas`, extraída en B1).
3. **Gasto COGS:** `ExpenseEntry` `category='INVENTORY'`, `expense_type=infer_expense_type(...)`, `amount = total del archivo`, `transaction_date = fecha del movimiento`, `payment_method` de la fila, `provenance='REAL'`, `product_id`, `source_upload_id`, `source_row_ref` iguales a los del movimiento (así una relectura no lo duplica y borrar el archivo lo anula), descripción «Stock inicial: X». Se arma con el mismo helper que la importación (extraído en B1).
4. Un `DecisionAuditLog` por entidad (`MEJORA_DE_DATOS:compra_catalogo_sin_costo`) con antes/después, total, medio de pago y, si corresponde, `autorizo_fecha_con_arqueo`. `DataRepairRun` por tanda (`repair_type='MEJORA:compra_catalogo_sin_costo'`).
5. Tras el commit: recálculo de score (`mock_score_trigger` en tests).

**Arqueo:** el `CashClose` es por día (`close_date`) y guarda un snapshot. Sólo importa si el medio es efectivo y existe un cierre del mismo día del gasto: la vista previa lo marca y la auditoría registra la autorización. Con `transfer` no toca el efectivo físico. El cierre guardado **no se reescribe** (invariante de `pos-offline.md`).

### Deshacer (condicional, idempotente — mismo patrón que ruta A)
- Gasto: se anula (`void_reason='MANUAL_ADMIN_VOID'`, no requiere migración) sólo si sigue vivo y sin `has_user_edits`.
- Movimiento: costo → NULL sólo si sigue como lo dejó la corrida; `recalcular_costo_vigente`.
- Ventas: `_restaurar` sólo si `_serializar(venta) == after_json`; si no, quedan pendientes con motivo.
- `cost_avg_*` no se restauran (mismo criterio que M·2d-2).
- Lápida: el `MEJORA_DE_DATOS_REVERTIDA` impide que el automático la reaplique (acá igual nunca es automática).

## PRs (cada uno desde `main`, doc de dominio en el MISMO commit)
**B1 — refactor sin cambio de comportamiento.** Extraer a `mejoras_de_datos/costeo_compartido.py` lo compartido de `compra_sin_costo.py` (`_costear_ventas`, restauración de ventas y de producto, fotos `_foto_movimiento`/`_foto_producto`) y, de `ingestion_import_service._add_catalog_initial_stock_cogs` (`:4188`), el armado del `ExpenseEntry` en una función pública que la importación sigue llamando. Red de seguridad: los 14 tests de `test_compra_sin_costo.py`, los 13 del mecanismo y los de importación de catálogo.

**B2 — la mejora + vista previa.**
- `mejoras_de_datos/compra_catalogo_sin_costo.py` y su alta en `registro._de_fabrica()` (import perezoso, como ruta A).
- Cambio de contrato (`necesita_archivo`, `evidencia`) y su uso en `servicio.aplicar`, antes del lock.
- `GET /data-improvements/{id}/preview` en `api/v1/mejoras_de_datos.py` (OWNER/ADMIN, tope de archivos por request, devuelve candidatos con total, unitario, fecha, medio, ventas a costear, aviso de arqueo, y los «a mano» con motivo). S3 por `S3Client()` creado perezosamente y reemplazable en tests.
- Doc en `docs/claude/plataforma.md` (sección «Mejoras de datos»).

**B3 — frontend.** `MejorasDeDatosAviso.tsx`: para mejoras `cambia_valores`, lista con vista previa (producto, cantidad, total, unitario, fecha, medio de pago, ventas que se costean, aviso de arqueo), «Aplicar» (PIN por el interceptor existente, ignorar `esPinCancelado`) y «Deshacer». Servicio `fetchVistaPrevia` en `services/mejorasDeDatos.service.ts`; tipos en `types/api.ts`; invalidación de `["data-improvements"]`, `["margins"]`, `["products"]`, `["health-scores"]` y las queries de gastos/caja. Visible sólo a OWNER/ADMIN, se oculta ante 404/5xx.

## Archivos críticos
- `backend/app/application/services/mejoras_de_datos/{contrato,servicio,registro,compra_sin_costo}.py`
- `backend/app/application/services/ingestion_import_service.py` (`_add_catalog_initial_stock_cogs`, `_costo_unitario_desde_total`, `_import_row_anchor`, `_source_row_ref`, `_PAGO_COLS`, `_resolve_stock_treatment`, `_filas_y_mapeo`)
- `backend/app/application/services/reread_service.py` (`_fresh_summary`), `backend/app/integrations/s3.py`
- `backend/app/application/services/cash_service.py` (`normalize_payment_method`)
- `backend/app/api/v1/mejoras_de_datos.py`
- `frontend/src/features/dashboard/MejorasDeDatosAviso.tsx`, `frontend/src/services/mejorasDeDatos.service.ts`, `frontend/src/types/api.ts`

## Tests (B2, más los de B1/B3)
- **Round-trip contra el import real (el más importante):** armar un xlsx de catálogo en memoria, importarlo por el camino real, dejarlo en el estado legado (costo NULL, gasto anulado/inexistente) y comprobar que la mejora **ubica la misma fila** que el import escribió. Con S3 falso. Cubre el riesgo de ctx/índice.
- Casos reales: Camisa (8) y Pantalón (15). Gasto = total exacto; unitario redondeado; ventas posteriores costeadas; medio de pago desde la fila; sin columna → `transfer`.
- No entran / van «a mano»: tratamiento «apertura»; hoja sin total; encabezado que no es de total; nombre o cantidad distintos; hash sin coincidencia o ambiguo; gasto vivo ya existente; archivo ausente en S3; medio `other`/`account`; tenant ajeno.
- Arqueo: efectivo + cierre del día → marcado y auditado; `transfer` → no marca.
- Deshacer: gasto anulado, costo y ventas restaurados; venta editada después → pendiente; idempotente; un `MEJORA_DE_DATOS_REVERTIDA` impide reaplicar solo.
- **Mutación:** sacar el recosteo de ventas y sacar la creación del gasto → los tests deben fallar.
- `-m postgres -n 0` contra `vektor_pgtest` (`DATABASE_URL` y `TEST_PG_DSN` locales): locks y SQL de la etapa 1 (LATERAL del último confirm, `FOR UPDATE OF` con join).

## Verificación
`ruff check .` y `mypy app` sin `| tail`; **nunca** `ruff format`; `git diff --stat` y `git diff --check` antes de commitear; `CLAUDE.md` < 40.000 caracteres; jest `--runInBand` para el aviso; CI verde. **Smoke en la cuenta del dueño (lo corre él; yo no tengo acceso a prod):** la vista previa lista Camisa y Pantalón con su total y el medio de pago de la fila; aplicar con PIN crea los dos gastos al 14-nov, deja costo «estimado» y costea las ventas; el banco y el margen se mueven; Deshacer lo devuelve todo.

## Riesgos y lo que no puedo verificar desde acá
- **Que el ctx/índice recomputado coincida con el guardado:** lo cubre el round-trip; en prod, la vista previa es el diagnóstico (fila no ubicada → «a mano», nunca se carga a ciegas).
- **Que el xlsx siga en S3** y que «Costo total» sea el total de la línea: la vista previa muestra total y unitario antes de confirmar.
- **Relectura posterior** del mismo archivo: el gasto lleva `source_row_ref` del producto, pero no probé que `reread` lo preserve sin duplicar ni anularlo; queda como chequeo explícito en B2 (test si es barato, si no smoke).
- **`MANUAL_ADMIN_VOID`:** verificar al implementar que ningún lector lo trate distinto de otro motivo de anulación.
- **Fecha real:** si el xlsx trae una fecha sin mapear, queda para un PR aparte.
- Sospecha abierta del 17-nov (stock inicial fechado en la carga y no en la compra) **no se resuelve acá**.

## Fuera de alcance
Fecha de compra leída del archivo; crear la mejora para `adjustment` (apertura) sin costo (ya lo cubre `opening-cost`); fusionar duplicados; lectura de S3 en el panel; aplicación automática.
