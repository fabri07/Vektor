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

### Fase 2 — el signo por fila con contexto, y clasificación por contenido
**Un negativo no es un error por sí mismo** (puede ser una pérdida real, un error de cálculo o de tipeo). Se evalúa DESPUÉS del mapeo efectivo (incluye correcciones del usuario) y según qué representa la columna:
- **Columna de resultado/saldo/flujo/diferencia** (no es un monto de operación, o no está mapeada): no se valida; nunca bloquea. Una pérdida es un dato.
- **Monto de una operación** (venta, gasto, compra) negativo → la fila va a «Otros» con un motivo que dice QUÉ parece, deducido del contexto de la fila, nunca corregido solo:
  - `parece_devolucion`: el tipo/concepto dice devolución, anulación, nota de crédito, reintegro.
  - `signo_probablemente_invertido`: `|monto|` coincide con cantidad × precio unitario de la misma fila.
  - `no_cuadra_con_la_fila`: hay cantidad y precio y el monto no coincide con ninguno de los dos signos (error de cálculo o tipeo).
  - `negativo_sin_contexto`: no hay de dónde deducir nada.
  Nunca se invierte el signo automáticamente (no-invention). Si en el futuro se soportan devoluciones, `parece_devolucion` es la que pasa a importarse.
- **Columnas derivadas con fórmula conocida** (margen = ventas − costo): si cuadra con las otras columnas de la fila, es una pérdida real; si no cuadra, se avisa como posible error de cálculo. Informativo: estas columnas no se importan (las métricas las calcula FactsService).
- **Hojas derivadas por contenido**, reusando `is_summary_or_derived`/`derived_detected`: el nombre deja de ser la única señal (y se ignora la numeración adelante: `"3. Resumen mensual"`); por contenido = período sin día (`YYYY-MM`), encabezados de totales/%/promedio/acumulado, y otra hoja del mismo archivo con movimientos fechados del mismo rango.
- **Libro mixto** con columna "tipo de movimiento": cada fila se rutea por su tipo (venta/gasto/compra); aportes y retiros de socios a «Otros» con motivo. Catálogo con columna proveedor → `product` con el proveedor como vínculo.
- **Prueba completa** con el archivo de ASTERIA anonimizado en el corpus: no duplica ventas (Σ ventas importadas == Σ de "Movimientos", la hoja resumen excluida), los pendientes quedan en «Otros» con su motivo, y ningún negativo de saldo bloquea.

### Fase 3 — fórmulas sin calcular
Evaluador propio, con especificación exigente y sin prometer "exacto como Excel":
- **Subconjunto declarado:** `+ - * /`, paréntesis, `^`, referencias A1 relativas/absolutas **de la misma hoja**, `SUM`, `ROUND` (half-away-from-zero, como Excel), constantes numéricas. Cualquier otra cosa → pendiente.
- **Semántica documentada:** `Decimal`; celda vacía = 0 sólo en aritmética (como Excel) y se registra; texto en aritmética, división por cero o error de Excel (`#N/A`, …) → pendiente, nunca 0.
- **Límites:** tiempo total (reloj de `LimitesDeArchivo`), cantidad de fórmulas, referencias por fórmula y profundidad de dependencias; **detección de ciclos** → pendiente.
- **Procedencia por celda:** `calculado_por_vektor` con la fórmula de origen; aviso con el conteo. Lo no soportado queda con la marca (va a «Otros» como hoy).
- Compuerta: corpus de fórmulas con su resultado de Excel escrito a mano.

## Decisiones tomadas
- Negativo en monto de operación → «Otros» con motivo contextual; nunca se corrige solo (dueño, 2026-10-05).
- Orden: persistencia → fila y clasificación → fórmulas (revisión externa).
