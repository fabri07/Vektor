# Asteria — destrabar la relectura y cerrar los 5 bloques operativos

## Contexto

La cuenta de Asteria (`agustinalahora4@gmail.com`, tenant `ef97804b-79a9-4c34-a50d-f83b3b9c9e77`)
está congelada desde el `2026-08-10 23:53`: esa es la última escritura de datos de
cualquier tipo. Diagnóstico read-only contra Neon del 2026-09-01:

- **No hay duplicación.** 1.939 ventas vivas / 0 anuladas / 1.939 `source_row_ref`
  distintos; 624 gastos vivos / 0 anulados. `1939 + 624 = 2563`, exacto el
  "2563 a actualizar / 2563 a anular" del dry-run. No se escribió una sola fila.
- **El apply nunca termina.** El mismo archivo tiene **3 relecturas muertas**:
  14/8 `RUNNING`, 18/8 `RUNNING`, 1/9 `APPLYING` (reclamada por un worker a las
  22:40:37, contadores en 0, sin conexión viva a la base 25 minutos después).
  La del 27/8 la canceló el usuario a los 7 minutos.
- Consecuencia: **ninguno de los Bloques 1–7 es verificable en esa cuenta.**

El cuello de botella es el apply. Las fases van en ese orden: hasta que la Fase 1
cierre, ninguna de las otras se puede verificar en Asteria.

### Lo que ya está probado y lo que es hipótesis

**Probado** (payload del run `f98d7b04`, parseado por el código deployado hoy):
Bloque 1 funciona — `Ganancias` (1.840) y `ganancias 2` (433) salen marcadas
`is_summary_or_derived: True` y van al bucket `derived_detected` (2.273 filas
exactas), no a `otros_detectados` (que quedó en 6).

**Hipótesis inicial, DESCARTADA por la medición.** Se sospechaba que el salto de
~15 s (preview) a >300 s (apply) era 20× y por lo tanto un N+1 obvio, y que el costo
extra eran las filas de `DataRepairItem`. Las dos cosas resultaron falsas:

- El preview **no es comparable**: `preview_reread` usa `_estimate_reread` (estimación
  en memoria) y **nunca llama a `_reconcile`**. Sus 15 s no dicen nada del apply.
- Los `DataRepairItem` son **413 statements, no ~5.100**: el ORM los batchea. Igual
  las ventas (1.939 filas en 4 `INSERT`). Los inserts nunca fueron el problema.

Lo que sí agregaba el apply, antes de medir, se creía que era:

| Delta apply-vs-dry-run | Dónde |
|---|---|
| ~5.100 filas `DataRepairItem` | `reread_service.py` — los `if not dry_run` de 983/996/1160/1205/1293/1345 |
| `return_details=True` | `reread_service.py:1282` |
| `stamp_product_updated_at=True` | `reread_service.py:1284` |
| El commit final | worker |

Candidato de N+1 ya localizado: `stock_service.void_movement:599` hace **~4
round-trips por movimiento** (`acquire_write_lock_shared` + `db.get(Product)` +
`_get_or_create_balance` + `flush()`), y el lock **ya se tomó** al tope de
`apply_reread` (`reread_service.py:2993`). Son 405 movimientos en Asteria.

Por eso: **medir antes de rediseñar.**

### Decisiones tomadas con el usuario

1. **Medir primero**, y recién con el número elegir el fix.
2. **Atomicidad todo-o-nada.** Se mantiene la transacción única y se sube el límite.
   Lotear con commits parciales rompe la garantía de que un fallo deja la cuenta
   intacta — precisamente la propiedad por la que hoy Asteria está sana.
3. **Invariante de "Otros"** (palabras del usuario): *"si no los selecciono en la
   relectura o cuando subo el archivo, no tiene que importar nada de lo no
   seleccionado"*. Esto reemplaza la idea de un script de limpieza one-off: la
   relectura misma es el mecanismo de reconciliación (ver Fase 2).

---

# Fase 1 — Destrabar el apply

## 1.1 Medir dónde se va el tiempo

**Objetivo:** un número por fase que diga si 300 s es corto o si hay un N+1.

Reusar el harness existente `backend/scripts/asteria_dryrun_bloque7.py` (descarga
el `ASTERIA_home_deco.xlsx` real de R2, Postgres descartable, `alembic upgrade head`,
tenant/archivo con UUID fijo, y el guard `_abort_if_prod_like` que aborta si la
`DATABASE_URL` apunta a un host administrado). Hoy ejercita `insert_confirmed_data`,
no `apply_reread`.

**Nuevo:** `backend/scripts/bench_reread_apply.py`, hermano del anterior, reusando
sus helpers (`_abort_if_prod_like`, `_download_asteria_file`, `_ensure_tenant`,
`_ensure_uploaded_file`, `_build_confirm_payload`):

1. Sembrar el estado de partida con `insert_confirmed_data` hasta los mismos
   volúmenes que prod (≈1.939 ventas / 624 gastos / 398 productos / 405 movimientos).
2. Correr `apply_reread` **real** (no dry-run), cronometrado.
3. Instrumentar con un listener `before_cursor_execute` de SQLAlchemy para contar
   **statements por fase**, no solo segundos — es lo que distingue "mucho trabajo"
   de "N+1". Fases: `_load_existing_records`, loop de void, borrado de fingerprints,
   loop de `void_movement`, `insert_confirmed_data`, escritura de `DataRepairItem`,
   commit final.
4. Correr también el dry-run en la misma base y corrida, para tener el delta
   **medido** y no inferido.

**Salida:** tabla `fase | segundos | statements`.

> Contra Postgres local la latencia por statement es ~0,1 ms; contra Neon ~30-50 ms.
> El **conteo de statements** es lo que se traslada a producción; los segundos
> locales son un piso, no una estimación.

## 1.2 Arreglar lo que muestre la medición — HECHO

**Medición (`scripts/bench_reread_apply.py`, Postgres descartable, archivo real):**

| | seg | statements |
|---|---|---|
| `apply_reread` | 22,68 | **5.367** |
| commit final | 19,48 | — |

| Función | llamadas | seg | stmts | **stmt/llamada** |
|---|---|---|---|---|
| `insert_confirmed_data` | 1 | 9,97 | 2.096 | 2.096 |
| `void_movement` | **405** | 9,71 | **3.239** | **8,0** |
| `_load_existing_records` | 1 | 0,23 | 2 | 2 |

**Hubo N+1, y era el 60% del total.** `void_movement` gastaba 8 statements por
movimiento, de los cuales **2 eran `pg_advisory_xact_lock_shared`** — uno propio y
otro de `_get_or_create_balance` — sobre un lock que `apply_reread` **ya tiene
tomado** y que solo se libera en el commit. 810 round-trips de puro peaje.

**Fix (Rama B):** parámetro `lock_held` en `void_movement` y
`_get_or_create_balance` (default `False` = comportamiento de siempre, ningún caller
existente cambia). Los dos bucles de void de `_reconcile` lo pasan en `True`.

**Resultado medido: 5.367 → 4.555 statements (−15%)**, advisory locks 812 → **2**,
y estado final byte a byte idéntico (1.939 vivas / 1.939 anuladas, 624 / 624, 405
movimientos, 9 en Otros).

**Descartado por la medición, no por opinión:** se probó además precargar los
productos del bucle en una query para que `session.get` pegara a la identity map.
El A/B dio **4.556 con precarga vs 4.555 sin ella** — `session.get` ya pegaba a la
identity map, y los 803 `SELECT products` son de `insert_confirmed_data`, no del
bucle de void. Se removió: dejarla habría sido un comentario que la medición
desmiente. **Queda como próxima frontera** si hiciera falta más: `insert_confirmed_data`
gasta 2.094 statements, con ~800 `UPDATE products` + ~800 `SELECT products`.

**Y también Rama A**, porque un N+1 resuelto no garantiza que el límite alcance: los
4.555 statements restantes son 137-228 s de latencia contra Neon. Ver 1.2b.

## 1.2b Límites de tiempo — HECHO

Nuevo módulo `app/application/services/reread_limits.py`, única fuente de tres
números que **tienen que moverse juntos**:

    soft (25 min) < hard (30 min) < stale (40 min)

- **soft → hard**: la ventana para hacer rollback y marcar `FAILED` cuando salta
  `SoftTimeLimitExceeded`. Los 30 s que había (270→300) no alcanzan para revertir
  ~5.000 filas contra Neon: llegaba el SIGKILL en el medio y el run quedaba zombie.
- **hard < stale**: `_STALE_RUNNING_AFTER_SECONDS` era 15 min contra un límite de 5,
  así que sobraba. Con el límite nuevo ya no: si el umbral fuera menor que el límite
  duro, **un apply todavía vivo se declararía muerto y arrancaría un segundo apply
  encima**. Subir el límite sin subir el umbral convierte un timeout en una carrera.

## 1.3 Que un run muerto no quede zombie — HECHO (ver 1.2b)

- **Soft timeout:** `SoftTimeLimitExceeded` es subclase de `Exception`, así que el
  `except Exception` del worker (`reread_worker.py:116`) **sí** la agarra e intenta
  rollback + marcar `FAILED`. El problema es que eso tiene que entrar en los 30 s que
  hoy separan `soft=270` de `hard=300`, y un rollback grande contra Neon se los come.
  **Fix:** ampliar la ventana (soft = T, hard = T + 120).
- **Hard kill (SIGKILL):** no hay excepción posible. La única red es el sweeper (1.4).

## 1.4 Sweeper de runs abandonados sin depender de Beat — HECHO

`jobs.sweep_stale_reread_runs` existe (`app/jobs/reread_sweep_worker.py`) y está en el
`beat_schedule` (`celery_app.py:128`), pero **beat no está desplegado** — por eso los
zombies del 14/8 y 18/8 siguen ahí un mes después.

1. **Red inmediata, sin infra nueva:** llamar `sweep_stale_reread_runs` al inicio de
   `reread_preview` y `reread_apply` (`app/api/v1/ingestion.py:3531` y `:3694`). Es un
   `UPDATE` acotado por `updated_at`, alcance global.
2. **Cierre correcto:** desplegar el servicio de beat — `backend/beat/railway.toml` ya
   está en el repo. Tarea de infra, fuera del código.

Los 3 runs zombie actuales de Asteria (`8516bebc`, `62731cdf`, `f98d7b04`) los levanta
solo el sweeper de la pieza 1 en cuanto se toque la relectura.

---

# Fase 2 — Lo no seleccionado no deja rastro

Implementa el invariante del usuario. **Reemplaza** la idea de un script de limpieza:
la relectura es el mecanismo, y así sirve para cualquier tenant, no solo Asteria
(memoria `feedback_systemic_not_per_tenant`).

## 2.1 La relectura descarta lo pendiente de contextos no incluidos

Hoy `_dismiss_matching_unclassified` (`reread_service.py:1029`) solo marca `DISMISSED`
las filas cuyo `row_ref` la relectura **sí importó**. Las 2.273 de `Ganancias`/`ganancias 2`
nunca se importan, así que ninguna relectura las toca: quedan `PENDING` para siempre.

**Cambio:** extenderla para que también descarte los `UnclassifiedRecord` `PENDING` del
archivo cuyo contexto de origen **no está incluido** en esta relectura (hoja derivada, o
desmarcada por el usuario). Motivo auditable propio, distinto del match por `row_ref`.

**Decisión abierta para la implementación — la clave del join.** `_capture_unclassified`
escribe `context_label` (`"Ganancias"`, `"ganancias 2"`), que coincide con el `label` de
`mapping_contexts`. Matchear por label funciona en este caso pero es frágil (dos hojas con
el mismo nombre, un rename). La alternativa es agregar una columna `context_id` a
`unclassified_records` (additive, con migración) y matchear exacto. **Recomiendo la
columna**: es el identificador que el resto del pipeline ya usa, y el label no es una
identidad. Lo confirmo con el usuario antes de escribir la migración.

Salvaguardas: solo toca `PENDING` (lo que el usuario ya clasificó está en `IMPORTED` y no
se toca); `DISMISSED` es un cambio de status, no un borrado, así que es auditable y
reversible.

**Nota honesta sobre el alcance del invariante.** Para hojas **derivadas/resumen** Bloque 1
ya hace exactamente lo que pedís (verificado). Para hojas **ambiguas sin entidad asignada**,
FASE F las captura a "Otros" *a propósito* ("en vez de descartarse en silencio",
`ingestion_import_service.py:4905`). Eso choca de frente con el invariante y es una decisión
de producto, no un bug: si "no seleccionado" también debe significar "no capturado" para las
ambiguas, se pierde la única red que hoy evita descartar datos en silencio. **Lo dejo
planteado, no lo cambio sin tu decisión explícita.**

## 2.2 "Importar todo lo sugerido" deshabilitado cuando no hay sugerencia

`otros/page.tsx:323` — el botón está siempre activo. En el estado actual de Asteria
(destino y categoría vacíos en todas las filas) dispara una importación masiva sin
sugerencias, que es exactamente lo peligroso que marcaste.

**Cambio:** deshabilitarlo cuando ningún registro pendiente tiene `suggested_entity`, con
un texto que explique por qué está deshabilitado (no un botón muerto sin motivo).

---

# Fase 3 — Habilitar los flags para Asteria

Los cuatro `*_ROLLOUT_TENANT_IDS` tienen `default_factory=list` en `app/config/settings.py:292-322`.
Verificado en la base: `ingestion_schema_decisions` = 0 y `product_supplier_links` = 0.

Habilitar para el tenant de Asteria, **uno por uno y verificando cada uno con una relectura
controlada** — no los tres de una, porque entonces un resultado raro no se puede atribuir:

1. `PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS` (Bloque 2 — "Tienda" → proveedor).
   Verificación: `product_supplier_links` > 0 y el producto repuesto desde dos tiendas
   deja de duplicarse.
2. `CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS` (Bloque 3A — compra+envío como costo final).
   Verificación: `unit_cost_ars` coincide con `compra+envío` en los productos que traen
   ambas columnas.
3. `INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS` (Bloque 5 — memoria de esquema).
   Verificación: `ingestion_schema_decisions` > 0 y la segunda relectura precarga el mapeo.

Es cambio de variables de entorno en Railway, no de código.

---

# Fase 4 — Corregir la categorización

Medido sobre los 398 productos reales de Asteria: la inferencia acierta **114 (28%)** con
confianza `high`, **0** con `medium`, 284 (71%) quedan sin categoría.

## 4.1 Mapear "Especificaciones" → `description`

`column_mapping_service.py:722` tiene el keyword set `{descripcion, descripción, detalle,
comentarios}` — **falta "especificaciones"**. Por eso los 398 productos tienen `description`
vacía, y por eso el nivel `medium` de la inferencia (que desempata con las especificaciones)
da 0 y **no puede dar otra cosa**. Agregar `especificaciones`/`especificacion` al set.

## 4.2 Ampliar el vocabulario de `decoracion_hogar`

`product_category_inference.py` — huecos confirmados contra nombres reales que hoy caen en
`low`: `alfombra`, `felpudo`, `canasto`, `cesto`, `organizador`, `dispenser`, `cafetera`,
`trapo`. La medición del 4.1 + 4.2 se rehace con el mismo método (función pura sobre los
nombres reales) para saber cuánto sube el 28% — número medido, no prometido.

## 4.3 El filtro dice la verdad

`products/page.tsx` ya carga el catálogo completo client-side (`:198`) y ya tiene el filtro
"Sin categoría" (`:301`), así que los contadores se calculan sin pedir nada al backend.

- Contadores por categoría en el desplegable: `Textiles (49)`, `Sin categoría (284)`.
- Ocultar o deshabilitar las categorías con cero resultados — hoy ofrece 7 categorías del
  catálogo del vertical (`product_categories.py:53`) que no matchean ningún producto, que es
  lo que te llevó a "Sin productos con ese estado".
- Tarjeta de "Sin categoría" visible junto a las otras métricas.

**Respetar el guard de truncado** (`possiblyTruncated`, `products/page.tsx:205`): si el
catálogo supera el techo de acumulación, los conteos son parciales y **no** se muestran como
si fueran totales (no-invention).

---

# Fase 5 — El preview explica lo que hace

## 5.1 "A actualizar / A anular" se lee como duplicación

`FileListSection.tsx:910-947`. Los dos números son el mismo conjunto visto dos veces: es un
reemplazo (anular la versión anterior, insertar la recalculada), no una suma.

**Cambio:** presentarlo como una sola frase de reemplazo —
> Se reemplazarán 2.563 registros existentes. No se duplican: se anula la versión anterior
> y se crea la recalculada.

manteniendo los contadores de lo que sí es distinto (nuevos, preservados, sin cambios).

## 5.2 Qué son las "~6.103 filas"

`RereadProgress.tsx:76` muestra `total_rows` del run. **Verificado:** 6.103 es la suma de
*todas* las hojas del archivo (1258+880+59+4+1840+433+1062+565+2) — **incluye las 2.273
derivadas que no se importan**. Decir "filas del archivo" y, mejor, mostrar las filas que
realmente se van a procesar.

---

# Verificación

**No declarar nada cerrado sin las cuatro** (memoria `feedback_ci_verification`).

1. **Harness local (Fase 1):** el apply completo sobre el archivo real termina; run en
   `APPLIED` con `sales_voided` ≈ 2.563 e `inserted` > 0; y una **segunda relectura idéntica
   da 0 duplicados** — la propiedad que las sesiones A/B del Bloque 7 probaron para el
   import y que acá hay que probar para el apply.
2. **Suites:** `test_reread_file.py` (68 tests) + suite de ingestión completa; la de stock si
   se toca `void_movement`; jest para los cambios de `otros/page.tsx`, `products/page.tsx`,
   `FileListSection.tsx`, `RereadProgress.tsx` (con `--runInBand` — memoria
   `feedback_jest_workers_flaky`). Todo test nuevo, mutation-testeado: revertir el fix y
   confirmar que falla (memoria `feedback_mutation_test_new_tests`).
3. **CI:** `ruff check .` + `mypy` + `pytest` con el mismo alcance que corre CI. Nunca
   `ruff format` (memoria `feedback_no_ruff_format`).
4. **Prod:** relectura real de Asteria y confirmar **en la base** que el run queda `APPLIED`
   con contadores > 0, que "Otros" baja de 2.282 a ~9, y que los productos con categoría
   suben de 0.

# Archivos

| Archivo | Fase | Qué |
|---|---|---|
| `backend/scripts/bench_reread_apply.py` | 1.1 | **nuevo** — medición |
| `backend/app/jobs/reread_worker.py` | 1.2-1.3 | límites y ventana soft/hard |
| `backend/app/application/services/stock_service.py` | 1.2 | solo si 1.1 confirma el N+1 |
| `backend/app/api/v1/ingestion.py` | 1.4 | sweeper en preview/apply |
| `backend/app/application/services/reread_service.py` | 2.1 | descarte de contextos no incluidos |
| `backend/app/persistence/models/unclassified_record.py` + migración | 2.1 | `context_id` (si se confirma) |
| `frontend/src/app/(protected)/otros/page.tsx` | 2.2 | botón deshabilitado |
| `backend/app/application/services/column_mapping_service.py` | 4.1 | keyword `especificaciones` |
| `backend/app/domain/product_category_inference.py` | 4.2 | vocabulario deco |
| `frontend/src/app/(protected)/products/page.tsx` | 4.3 | contadores + cero-resultados |
| `frontend/src/features/ingestion/FileListSection.tsx` | 5.1 | copy de reemplazo |
| `frontend/src/features/ingestion/RereadProgress.tsx` | 5.2 | qué son las filas |

Una migración additive (Fase 2.1) si se confirma `context_id`. Fase 3 es solo variables de
entorno en Railway.

# Orden y compuertas

Fase 1 → **verificar en prod que una relectura completa** → Fase 2 → Fase 3 (un flag por vez)
→ Fases 4 y 5 (independientes entre sí, se pueden hacer en paralelo).

No arrancar una fase sin cerrar la anterior con su verificación (memoria
`feedback_subagent_phase_gates`). Las Fases 2-5 no se pueden validar en Asteria hasta que la
Fase 1 permita que una relectura termine.
