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

---

# Estado — 2026-09-02 (tarde)

## Fase 1: CERRADA y verificada en producción

La relectura que el usuario disparó el 2026-09-02 **terminó**:

```
89071abe  APPLIED  16:49:23 → 17:11:04   (21m 41s)
          sales_detected=2563  sales_voided=2563
```

Reemplazo limpio contra la base real: 1.939 ventas vivas / 1.939 anuladas,
624 / 624 gastos, 405 movimientos, **0 duplicados por `source_row_ref`**. Los 3
runs zombie quedaron en estado terminal (`stale_timeout` / `stale_legacy_running`),
o sea que el sweeper de 1.4 hizo su trabajo.

El usuario cerró la ventana a los 10m39s; cerrarla no cancela, y el aviso del
modal ("se sigue procesando igual") era correcto.

**Deuda:** 21m41s contra un límite blando de 25 min es 13% de margen. La próxima
frontera medida es `insert_confirmed_data` (2.096 statements, ~800 `UPDATE` +
~800 `SELECT` de productos). No se toca en esta tanda.

## Por qué "no cambió nada" aunque la relectura terminó

Tres causas medidas contra Neon, ninguna de ellas un fallo del apply:

1. Los 2.288 pendientes de "Otros" no los limpia ninguna relectura (Fase 2, abajo).
2. Los 398 productos tienen `description` vacía → la inferencia de categoría no
   puede pasar del nivel `low` (Fase 4).
3. Los tres `*_ROLLOUT_TENANT_IDS` siguen en `[]`: los Bloques 2/3A/5 no corrieron.

> `prod_created=0 / prod_updated=0` NO prueba que el apply no hiciera nada:
> también es el resultado esperado si los productos ya existían y no cambiaron.
> No se usa como evidencia.

## Composición real de "Otros" (2.288, medido)

```
Ganancias                        PENDING  1840
ganancias 2                      PENDING   433   → 2.273 (99,3%)
LD 2026 — Movimientos ambiguos   PENDING     8
LD 2025 — Movimientos ambiguos   PENDING     4
Fila sin fecha reconocible       PENDING     3

de esos, filas TOTALMENTE vacías:            314
con `suggested_entity`:                        3
```

## Fase 2 — implementada (rama `fix/otros-no-acumula-lo-no-importado`)

Invariante, en la forma más ancha que pidió el usuario:

> Un contexto no seleccionado o detectado como resumen/derivado no debe crear
> ventas, gastos, productos, movimientos ni registros en "Otros". En una
> relectura también deben descartarse sus registros pendientes antiguos.

1. **`unclassified_records.context_id`** (mig `20260902_0001`, additive, nullable,
   sin backfill) + índice `(uploaded_file_id, context_id)`. El label no es una
   identidad: en el camino multi-hoja es el nombre de la hoja y en la docena de
   capturas por fila es un MOTIVO en castellano.
2. **El import ya no captura lo excluido** (`ingestion_import_service`, camino
   multi-hoja): el guard va ANTES de la captura, porque `_hoja_incluida` sólo
   protegía a las hojas cuya entidad está en `entity_bucket` — una hoja
   desmarcada *e* inclasificable nunca lo alcanzaba.
3. **La relectura descarta lo pendiente** de contextos que no importó
   (`_descartar_pendientes_de_contextos_no_importados`), con motivo propio
   (`hoja_derivada` / `hoja_no_seleccionada`), distinto del match por `row_ref`.
   Devuelve **conteo por hoja y motivo** (`RereadApplyResult.otros_descartados`)
   en vez de limpiar en silencio; en preview cuenta sin mutar.
4. **Filas en blanco**: el guard mira los VALORES, no las claves. Opt-out
   explícito (`skip_blank_rows=False`) para la captura por riesgo de columna,
   donde el vacío ES lo que se reporta.
5. **"Importar todo lo sugerido"** deshabilitado con motivo VISIBLE cuando no hay
   ninguna sugerencia. El conteo es global (`/others/count` ahora devuelve
   `pending_suggested`): el botón opera sobre todos los pendientes, así que
   decidir con las 50 filas de la página se equivoca en las dos direcciones.

### Límite deliberado del descarte automático

"No seleccionada" es una decisión **explícita** (`context_confirmed[cid] is False`),
no la ausencia de decisión. Sin `context_confirmed`, `context_is_included` cae al
gating por tipo y una hoja ambigua (sin entidad) daría "no incluida" SIEMPRE: el
descarte se llevaría puesta la red de FASE F, que es justo la que evita perder en
silencio una fila que el parser no supo leer. El usuario eligió acotar el descarte
a las hojas derivadas y conservar esa red. Cubierto por
`test_hoja_ambigua_sin_decision_sigue_capturando`.

### Verificación (criterio determinista, no "~15")

Después de una relectura, los únicos pendientes que pueden quedar son los de
contextos **incluidos** que son genuinamente ambiguos. Para ASTERIA: los 12 de
"Movimientos ambiguos" + los 3 sin fecha; los 2.273 de `Ganancias`/`ganancias 2`
se van con motivo `hoja_derivada`, y ninguna fila vacía puede volver a entrar.

Tests (todos mutation-testeados — se revirtió cada fix y se confirmó que fallan):

| Test | Qué fija |
|---|---|
| `test_hoja_desmarcada_no_deja_absolutamente_ningun_rastro` | el invariante completo, a nivel import |
| `test_hoja_derivada_no_captura_aunque_no_haya_decision_explicita` | derivada sin decisión del usuario |
| `test_hoja_ambigua_sin_decision_sigue_capturando` | la red de FASE F NO se pierde |
| `test_filas_en_blanco_no_materializan_pendientes` | las 314 vacías |
| `test_reread_descarta_pendientes_de_hoja_no_seleccionada` | descarte por `context_id` Y por label legacy |
| `test_segunda_relectura_no_revive_los_pendientes_descartados` | idempotencia entre corridas |
| `test_label_ambiguo_no_descarta_nada` | el fallback por nombre no se pasa de rosca |
| `otros_bulk_import_gate.test.tsx` (2) | el botón y su motivo visible |

## Fase B (ex Fase 4 de inventario) — CONDICIONADA, no implementada

El usuario pidió confirmar tres cosas antes de tocar el chequeo temporal:

1. Qué fecha representa realmente el stock del archivo.
2. Por qué hay ventas hasta `2026-12-30`, posteriores a la relectura del `2026-09-02`.
3. Que el mensaje provenga efectivamente de `replay_timeline()` y no sólo de las
   560 compras bloqueadas por falta de cantidad.

Lo medido hasta acá: los 405 movimientos son `catalog_initial_stock | adjustment`,
todos fechados `2026-09-02`, contra ventas de `2025-02-15` a `2026-12-30`; y las
324 compras de mercadería son montos globales ("compra mercadería $1.053.240") sin
producto ni unidades.

## Fase C — categorización de productos (implementada)

**Medido antes y después sobre los 398 productos reales**, con la función pura
sobre los nombres de la base:

|  | antes | después |
|---|---|---|
| `high` (se aplica) | 114 (28%) | **180 (45%)** |
| `medium` (se sugiere, no se aplica) | 0 | 0 |
| `low` (sin categoría) | 284 (71%) | 218 (54%) |
| de los `low`: sin ningún keyword | 280 | 213 |
| de los `low`: ambiguos (≥2 categorías) | 4 | 5 |

**Corrección al diagnóstico del plan original.** Decía que el `medium` daba 0
"porque la `description` está vacía". Es falso por dos motivos, los dos
verificados en el código:

1. El texto de especificaciones YA llegaba a `infer_category`: sin columna
   `description` mapeada, `_specs_raw` cae a la heurística
   `_ESPECIFICACIONES_COLS`, que sí contiene "especificaciones".
2. El `medium` **no se aplica por diseño**: `insert_confirmed_data` sólo asigna
   la categoría con confianza `high`; la `medium` se guarda en `custom_fields`
   como sugerencia para revisión humana (Bloque 5). Es la regla de no-invención
   funcionando, no un bug.

La palanca real era la **cobertura del vocabulario**: 280 de 284 no matcheaban
ningún keyword. Nada que ver con el `medium`.

**Segunda corrección — el keyword iba en el motor equivocado.** El plan apuntaba
a `column_mapping_service.py:722`, pero ese set alimenta `_heuristic_match`, que
**sólo lo llaman los tests**: es el motor VIEJO, conservado como foto de
caracterización para el rediseño F-M. El motor vivo es `read_header` →
`analyze_header`, cuyo vocabulario de conceptos está en
`app/domain/header_semantics.py`. Editar el set viejo no habría cambiado nada en
la aplicación.

Cambios:

1. `especificaciones`/`especificacion` → concepto `descripcion` en
   `header_semantics.py`. `RESOLUCION["product"]["descripcion"]` ya llevaba a
   `description`, así que la columna ahora se persiste (antes: 0 de 398).
2. Vocabulario `decoracion_hogar`, elegido palabra por palabra contra los
   nombres reales: TEXTILES `alfombra`, `frazada`, `repasador`; BAZAR `bandeja`,
   `frasco`, `huevera`, `aceitero`, `especiero`, `salero`, `batidor`,
   `espatula`, `utensilio`, `hermetico`, `molde`, `medidora`, `cafetera`,
   `tabla`. Los 67 productos que se categorizan por estas palabras se revisaron
   uno por uno.
3. La batería de caracterización de encabezados suma la fila
   `("product", "Especificaciones")` con el veredicto del motor viejo (`FALTA`) y
   su lectura declarada en `LECTURA_NUEVA` — el diff entre motores sigue siendo
   explícito, fila por fila.

**Lo que queda afuera a propósito** (y no es un olvido): `canasto` (19), `cesto`
(11) y la familia `porta*` (27) son artículos de ORGANIZACIÓN, y el catálogo de
`decoracion_hogar` no tiene esa categoría. Meterlos en DECO o BAZAR sería elegir
por el negocio. Es un hueco del CATÁLOGO del rubro, no del vocabulario, y se
decide aparte. Fijado por `test_articulos_de_organizacion_siguen_sin_categoria`.

**Precisión, dicha entera:** de los 67 nuevos, 65 son inequívocos. Dos son
discutibles — "porta repasadores doble" y "cuelga repasador p/puerta" son
soportes, no textiles, y entran por `repasador`. Se dejan: dentro de este
catálogo no hay una categoría mejor para un colgador de repasadores, y el error
es de vecindad, no de concepto.

## Fase B — las tres confirmaciones, y una hipótesis mía que se cayó

### 1. Qué fecha representa el stock del archivo

La hoja `precios y stock ` tiene los encabezados `Tienda | Productos |
Especificaciones | Stock | Precio de compra | % Envio | compra+envio | Precio de
lista | col_8 | Precio de venta final`: **no hay ninguna columna de fecha**. O
sea que el archivo no fecha su stock, y los 405 movimientos
`catalog_initial_stock` quedaron con la fecha de la CORRIDA (`2026-09-02`), no
con una del negocio. El usuario confirmó que ese número es el stock de HOY.

### 2. Las 7 ventas posteriores al 2026-09-02

Son **7 de 1.939 (0,36%)**, todas entre el 26 y el 30 de diciembre de 2026:

    2026-12   7      <- las futuras
    2026-08  13
    2026-07  67
    2026-06 136 ... serie continua hacia atrás ...
    2025-12  55
    2025-03   1
    2025-02   1

El patrón: diciembre de 2026 no tiene ninguna otra venta, diciembre de 2025
tiene 55, y los extremos (2025-02 y 2025-03) tienen 1 venta cada uno. Es
consistente con un año mal leído sobre una fecha parcial, no con ventas
genuinamente futuras — pero **no está probado**: confirmarlo exige mirar esas 7
filas en el Excel. Los gastos, en cambio, terminan el `2026-07-11`, sin cola
futura.

### 3. De dónde sale el mensaje — MI HIPÓTESIS ERA FALSA

Sostuve que el ancla fechada hoy hacía que el replay diera negativo para todos
los productos. **Medido contra prod, es falso.** Corriendo
`check_products_temporal_divergence` read-only sobre los 161 productos con
ventas vinculadas:

    checked            158
    skipped_no_anchor    3
    divergences          9      <- 5,7%, no 158

`replay_timeline` arranca del stock ACTUAL y va restando: solo da negativo en
los productos cuyas ventas superan el stock de hoy. No es una alarma universal.

Los tres avisos son superficies DISTINTAS y ninguno viene de las 560 compras
bloqueadas:

| Mensaje | Origen | Número real |
|---|---|---|
| "…quedan con stock negativo en algún momento" | `impacto_inventario` del confirm | **66 de 161** (10/8) |
| "…registra ventas por fecha que superan el stock reconstruible" | `check_products_temporal_divergence` | **9 de 158** (hoy) |
| "Compras bloqueadas (falta columna de cantidad): 560" | `estimate_unlinked_products`, panel del PREVIEW | 560 |

Los 161 productos del `impacto_inventario` traen `compradas: 0` **todos**, y 66
terminan en negativo. Ese es el aviso ruidoso que vio el usuario.

### Qué queda por hacer, entonces

No lo que estaba planeado. `_candidate_products` y la fecha del ancla **no se
tocan**: el chequeo no está gritando: reporta 9 de 158, que es información
razonable. Lo que falla es el ENCUADRE — decir "faltan compras anteriores,
revisá las fechas de compra" cuando la situación real es que el archivo no trae
cantidades compradas de ningún producto y el inventario no se puede reconstruir
desde él. Es un cambio de mensaje (la opción que eligió el usuario: informar sin
alarmar), no de cálculo.

**Sin implementar. Requiere la decisión del usuario con estos números a la
vista.**
