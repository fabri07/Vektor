# Ingesta: archivos generados por script, hojas derivadas y nulos en JSONB

Origen: `ASTERIA_home_deco_ordenado.xlsx` (2026-10-05) — el worker falló dos veces
(`UntranslatableCharacterError: \u0000 cannot be converted to text`) y el archivo
quedó `FAILED` con el mensaje crudo de asyncpg. Reproducido en local con
`parse_uploaded_content` sobre el archivo real.

## Hallazgos (medidos sobre el archivo)

| # | Qué pasa | Evidencia | Alcance |
|---|----------|-----------|---------|
| H1 | La marca `FORMULA_SIN_RESULTADO` es `"\x00formula-sin-resultado\x00"`; Postgres no acepta `\u0000` en JSONB ni en TEXT. Todo archivo con fórmulas sin calcular revienta al guardar el resumen. En SQLite pasa, por eso ningún test lo vio. | 3.250 nulos en el summary, todos la marca | Todo `.xlsx` generado por script/LLM o nunca recalculado. Además cualquier `\x00` real (CSV/PDF mal codificados) rompe igual. |
| H2 | 1.601 celdas con fórmula sin resultado en 5 columnas de "1. Precios y stock" (`Envio`, `compra+envio`, `Precio de lista 80%/40%`, `Precio de compra`). Son aritmética simple: `=(E4+F4)`, `=G4+(G4*H$2)`, `=((E4/169800)*F$3)`. | openpyxl `data_only=False` vs `True` | Esas filas terminarían en "Otros" aunque se arregle H1. |
| H3 | El ValidationGate rechaza el archivo ENTERO por `negative_amount`, mirando CUALQUIER columna numérica del bucket de ventas, antes de que exista un mapeo. Los 11 negativos son `Flujo neto de caja $` y `Saldo de caja acumulado $`: legítimos, no son montos de venta. | `_extract_amounts` sobre `ventas_detectadas` | Cualquier planilla con una columna de saldo/resultado/diferencia. |
| H4 | "3. Resumen mensual" (Período `YYYY-MM`, totales, %, promedios, saldo acumulado) se clasifica como ventas. Importarla duplicaría las ventas de "2. Movimientos". | `mapping_contexts`: `sale`, 22 filas | Toda planilla con hoja de resumen/tablero junto a la de movimientos. |
| H5 | "2. Movimientos" es un libro mixto con `Tipo de movimiento` (venta, aporte de socio, gasto…) y se clasifica como `product`; "1. Precios y stock" (catálogo con columna proveedor) como `supplier`. | `mapping_contexts` | Libros mixtos y catálogos con columna de proveedor. |
| H6 | El error de base llega al usuario como texto de asyncpg. | `_record_failure(str(exc))` | Cualquier fallo de persistencia del worker. |

## Verificado contra el código (revisión externa, 2026-10-05)

- El worker NO usa `persistence/db/engine.py`: `_build_async_session` (`ingestion_worker.py:479`) crea su propio engine, y hay 18 `create_async_engine` en `app/`. Una protección en un solo engine no cubre el caso que falló.
- La detección de hojas derivadas ya existe (`is_summary_or_derived`, `derived_detected`, excluida por defecto) pero es **por nombre**: `_DERIVED_SHEET_PREFIXES = ("ganancia","resumen","balance")` con `startswith`. "3. Resumen mensual" no matchea por el "3. " adelante.
- `_record_failure` recibe un resumen mínimo (`{"error", "file_type"}`), así que el `FAILED` sí se escribe; el problema es exponer `str(exc)`.

## Plan corregido — orden: persistencia → fila y clasificación → fórmulas

### Fase 1 — persistencia y errores ✅ (rama `fix/ingesta-nulos-jsonb`)
- **F1a Estado explícito de la fórmula, primero.** La marca deja de ser un texto con `\x00`: pasa a una marca sin nulos (`formula-sin-resultado`) y `es_formula_sin_resultado` reconoce **la nueva y la vieja**. Se hace ANTES de cualquier normalización, para que limpiar nulos no borre la identidad de la marca vieja (summaries ya guardados en SQLite/tests, payloads en vuelo). A futuro: representar el estado por celda fuera del valor (`formulas_sin_resultado` por coordenada), sin depender de un texto especial.
- **F1b Normalizar SÓLO `U+0000`, nunca "no imprimibles" en general.** En `parse_uploaded_content` (punto único de los 5 caminos): se quita `\x00` de celdas, encabezados y nombres de hoja; el archivo original en S3 no se toca; se registra qué cambió (`summary["normalizaciones"]`: hoja, fila, columna, cuántos) y se avisa. **Colisión de encabezados:** si dos encabezados distintos quedan iguales tras limpiar, se desambiguan como hoy los duplicados (sufijo) y se avisa; nunca se fusionan columnas.
- **F1c Red de salida en TODOS los engines.** Un único `crear_engine()` en `persistence/db/engine.py` con `json_serializer` que **rechaza** (no limpia en silencio) un `\x00` con un error de dominio que nombra la ruta del JSON; las 18 llamadas pasan por él, incluido el worker. Compuerta estructural: ningún `create_async_engine` fuera de esa función.
- **F1d Error de persistencia del worker** → `FAILED` con motivo en castellano + `error_code` (`RESULTADO_NO_PERSISTIBLE`), `str(exc)` sólo al log/Sentry.
- **F1e El gate deja de mirar signos.** La regla 3 se saca del ValidationGate (corre antes del mapeo, sobre columnas que no se van a importar). El signo se valida en la Fase 2, por fila y sobre el mapeo efectivo.
- Compuertas `-m postgres` que recorren **el guardado real del worker** (`_save_result` y el camino del gate) con: fórmula sin calcular, `\x00` real en celda y en encabezado, colisión de encabezados.

### Fase 2 — clasificar por contenido y explicar cada fila que no entra ✅ (rama `fix/ingesta-clasificacion-por-contenido`)

**Diagnóstico medido sobre el archivo (2026-10-05):**
- "1. Precios y stock" → `proveedores`: la regla -0.5 (maestro) se dispara con `Proveedores` y sólo la frena `has_catalogo_fuerte` (sku/código/artículo/ítem); ni `Productos` ni `Stock` cuentan.
- "2. Movimientos" → `stock`: la regla 0 (compra de mercadería + cantidad) gana con `Mercadería recibida $` + `Cant.`. Es un **libro mixto**: la columna `Tipo de movimiento` trae 14 valores (Venta 2.209, Mercadería recibida 447, Compra de mercadería 80, Venta (costo sin registrar) 66, Flete/envío 61, Retiro de socios 23, Gasto operativo 20, Aporte de socio 17, Packaging/insumos 10, Devolución de proveedor 8, Publicidad 7, Préstamo a socio 4, Ajuste 4, Saldo inicial 1) y **cada tipo llena exactamente una columna de flujo** (`Venta cobrada $` | `Salida de caja $` | `Otro ingreso de caja $` | `Mercadería recibida $`); el resto (margen, reinversión, distribuible, saldo) es derivado.
- "3. Resumen mensual" → `ventas`: la detección de derivadas es por prefijo de nombre y `"3. Resumen…"` no matchea.
- `Origen del dato`: **389 filas "simulado"** junto a 2.568 "registro original (LD 2025/2026)".
- El archivo reorganiza los Libros Diarios 2025/2026 que el tenant YA importó; sin clave fuerte, importarlo duplica ~2.500 operaciones (`import_overlap_service` sólo avisa después).

**F2a Catálogo con columna de proveedor.** Una columna de producto (`producto`) o de existencias (`stock`) es evidencia de catálogo y excluye la regla de maestro: un maestro de proveedores no lista productos. El proveedor queda como vínculo (`supplier:name`, con su compuerta).

**F2b Hojas derivadas por contenido** (reusa `is_summary_or_derived`/`derived_detected`): (1) el nombre se compara sin la numeración adelante (`"3. Resumen mensual"`); (2) por contenido, aunque el nombre no diga nada: columna de período sin día (`YYYY-MM` / "mes"), mayoría de encabezados de agregado (total, promedio, %, acumulado, margen, ticket, operaciones) y **otra hoja del mismo archivo con movimientos fechados**. Las dos señales juntas; una sola no alcanza (una hoja de ventas mensuales sin otra de movimientos es legítima).

**F2c Libro mixto: ruteo por fila según el tipo.** Se detecta una columna cuyo encabezado dice tipo/movimiento/concepto-de-movimiento y cuyos VALORES son mayormente tipos reconocibles. Vocabulario versionado (`domain/tipo_de_movimiento.py`), por palabras y no por igualdad:
| Tipo (ejemplos) | Destino |
|---|---|
| venta, venta (costo sin registrar), cobro | venta |
| gasto operativo, flete/envío, publicidad, packaging/insumos, servicios, alquiler, sueldos | gasto (categoría inferida por el texto) |
| compra de mercadería | gasto COGS (compra) |
| mercadería recibida | «Otros» — ingreso de stock **sin pago**: es la otra cara de una compra; importarlo además de la compra duplicaría el costo |
| retiro de socios | «Otros» (sugerido: retiro de ganancias, que ya existe como operación) |
| aporte de socio, préstamo a socio, devolución de proveedor, ajuste, saldo inicial | «Otros» con su motivo (movimientos de capital/caja que hoy no son operaciones) |
| tipo desconocido | «Otros» — nunca se adivina |
El **monto** de cada fila es la ÚNICA columna de flujo con valor para ese tipo (venta → la de venta; gasto/compra → la de salida). Si hay cero o más de una con valor, la fila va a «Otros» con el motivo. Las columnas derivadas (margen, saldo, reinversión) nunca se importan.

**F2d Filas no reales.** Una columna de procedencia (`origen`, `fuente`, `origen del dato`) con valores simulado/estimado/proyectado/ejemplo/prueba → esas filas no se importan como reales: van a «Otros» con el motivo. Invariante de no-invención.

**F2e El signo, con contexto** (sobre el mapeo efectivo): sin cambios respecto de lo ya decidido — `parece_devolucion` / `signo_probablemente_invertido` / `no_cuadra_con_la_fila` / `negativo_sin_contexto`, nunca corregido solo; columnas de saldo/flujo/derivadas no se validan.

**F2f Archivo que repite lo ya cargado** — aviso ANTES de confirmar: comparar fecha+importe del archivo contra lo ya importado del tenant (la misma señal de `import_overlap_service`, adelantada al preview) y, si la coincidencia es masiva (ej. > 50 % de las operaciones), decirlo arriba de todo con el archivo de origen. Sigue sin bloquear ni descartar: sin clave fuerte, coincidir no prueba duplicado.

**Compuerta:** prueba completa con el archivo de ASTERIA anonimizado en el corpus (sin nombres de clientes/proveedores): clasificación de las 4 hojas, Σ ventas importadas == Σ `Venta cobrada $` de las filas reales tipo venta, el resumen excluido, simuladas y capital en «Otros» con motivo, ningún saldo negativo bloquea, y el aviso de repetición.

### Fase 3 — fórmulas sin calcular ✅ (rama `fix/ingesta-formulas`)
Evaluador propio, con especificación exigente y sin prometer "exacto como Excel":
- **Subconjunto declarado:** `+ - * /`, paréntesis, `^`, referencias A1 relativas/absolutas **de la misma hoja**, `SUM`, `ROUND` (half-away-from-zero, como Excel), constantes numéricas. Cualquier otra cosa → pendiente.
- **Semántica documentada:** `Decimal`; celda vacía = 0 sólo en aritmética (como Excel) y se registra; texto en aritmética, división por cero o error de Excel (`#N/A`, …) → pendiente, nunca 0.
- **Límites:** tiempo total (reloj de `LimitesDeArchivo`), cantidad de fórmulas, referencias por fórmula y profundidad de dependencias; **detección de ciclos** → pendiente.
- **Procedencia por celda:** `calculado_por_vektor` con la fórmula de origen; aviso con el conteo. Lo no soportado queda con la marca (va a «Otros» como hoy).
- Compuerta: corpus de fórmulas con su resultado de Excel escrito a mano.

## Decisiones tomadas
- Negativo en monto de operación → «Otros» con motivo contextual; nunca se corrige solo (dueño, 2026-10-05).
- Orden: persistencia → fila y clasificación → fórmulas (revisión externa).
- Fase 2 (dueño, 2026-10-05): filas que el archivo marca como simuladas → «Otros» con motivo (nunca entran como reales; en una cuenta de prueba se pueden importar desde «Otros» a propósito). «Mercadería recibida» → «Otros». Archivo que repite masivamente lo cargado → aviso + **confirmación explícita exigida por el backend**. Un solo PR.
