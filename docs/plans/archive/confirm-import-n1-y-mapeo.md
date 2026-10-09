# Confirm/import: el N+1 de savepoints y el mapeo que nadie revisaba (2026-09-03)

Dos problemas de una misma pantalla, resueltos en una PR.

## 1. El confirm tardaba 18m33s para 2.994 filas

El parseo de las 3.830 filas del mismo `.xlsx` (270 KB) tarda 11,9 s, así que el
cuello estaba después. **No se supuso dónde**: el bench de la relectura ya había
medido `insert_confirmed_data` sobre ESE archivo en 2.096 statements, que no
explican 18 minutos salvo con una latencia por statement absurda — el cuello podía
estar en cualquiera de las otras diez etapas del endpoint.

`scripts/bench_confirm_import.py` corre `confirm_file` REAL (el endpoint entero,
no sólo el importador) contra Postgres descartable con el `.xlsx` real de R2, y
cuenta statements por FORMA de SQL. Baseline: **3.250 statements / 32,8 s**, con
**794 SAVEPOINT + 794 RELEASE = 1.588 (48,9%) de peaje puro**.

### Causa

`guarded_savepoint` cuesta ~4 round-trips (flush previo + SAVEPOINT + DML +
RELEASE) y el import abría **dos por fila que crea producto**: uno en
`add_product_or_reuse` y otro en la rama de balance nuevo de
`_record_stock_movement`. Peor: ese flush previo **drena todo lo pendiente**, así
que el batch de 500 filas nunca agrupaba nada y cada producto, cada balance y cada
movimiento salía además en su propio INSERT.

### Correcciones (cada una con A/B del mismo bench)

| | statements |
|---|---|
| baseline | 3.250 |
| lote de altas + balances diferidos | 563 |
| + precarga temporal (2 queries por PRODUCTO → 2 en total) | 250 |
| re-import idéntico: 758 → | **47** |

Con la flag `PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS` activa —que es lo que hace
que "Tienda → Proveedor" se use de verdad— aparecía un tercer N+1
(`link_product_to_declared_supplier`: 251 SELECT + 238 INSERT por fila): **1.010
→ 330**.

### Lo que hace seguro al lote

`ProductCreateBatch` invierte el orden: **el post-trabajo de un alta no corre hasta
que la identidad final está resuelta**. Si el flush descubre que otra transacción
ocupó el SKU, el movimiento de stock, el vínculo y la fila del ledger tienen que
hablar del producto que quedó, no del descartado; correrlos antes obligaría a
remapear la sesión entera. Lo único que se registra al encolar son los índices EN
MEMORIA —si esperaran al flush, dos filas del mismo catálogo con la misma
identidad encolarían dos altas del mismo producto, que es una regresión de dedup—
y `_remapear_indices_de_producto` los corrige ante una sustitución.

Guarda: una fila que resuelve contra un producto todavía ENCOLADO fuerza el flush
antes de tocarlo, porque el merge escribe un movimiento que lo referencia por FK.

## 2. El camino principal importaba sin revisar el mapeo

`FileUploadSection` tenía un confirm propio —la **tercera** implementación del
confirm en el frontend— que mandaba `column_mappings: []`. Sin mapeos el backend
cae a la heurística de encabezados, así que `Especificaciones → description` y
`Tienda → supplier:name` no llegaban NUNCA por ese camino. Y es el que el usuario
toma por defecto: el panel aparece arriba apenas termina de subir, con un botón
listo, mientras el `ColumnMapperPanel` queda colapsado en una fila de la tabla de
abajo. Por eso el último import dio `description: 0` y un único proveedor
centinela, con el soporte ya implementado y probado.

Se reemplaza por el MISMO `ColumnMapperPanel`, con el bucket del tipo detectado
tildado y a la vista. (La primera versión de esto **sumaba** el detectado encima
del `ventas: true` fijo que ya tenía el panel, para no cambiar en silencio QUÉ se
importa al unificar los dos caminos; la revisión de abajo lo dejó derivando SOLO
de lo detectado, que es lo correcto.)

## Compuertas nuevas

- **Presupuesto ABSOLUTO de statements por forma de SQL**, no un ratio: un ratio
  sólo detecta crecimiento superlineal y un N+1 LINEAL de ocho queries por fila lo
  pasa sin despeinarse. El ratio queda como red secundaria contra un O(n²).
- **El camino de excepción del lote**: no pierde el resto, no fusiona identidades
  distintas, distingue qué restricción falló, y no deja transients huérfanos.
- Test de `FileUploadSection` — no existía ninguno, y ése es el hueco por el que
  entró el bug.

## Segunda pasada: revisión del diff (2026-09-04)

Cuatro hallazgos sobre el código ya subido. Los cuatro con test; los dos
primeros, mutation-testeados contra el comportamiento viejo.

**El alta que pierde la carrera arrastraba a la fila siguiente.** El lote
descarta el alta encolada cuando otra transacción ocupó el SKU, y su
post-trabajo recibe el producto que quedó. Pero el import guarda esa alta en sus
índices EN MEMORIA, y la fila siguiente con la misma identidad la encontraba
ahí: forzaba el flush —correcto, el merge escribe un movimiento que la
referencia por FK— y después NO volvía a leer el índice. Seguía apuntando al
transient descartado, así que el merge se perdía y el movimiento salía con el
`product_id` de un producto que nunca se insertó. Verificado contra Postgres:
`ForeignKeyViolationError` en `inventory_movements_product_id_fkey`, o sea el
confirm entero. El fix es re-leer el índice después del flush (que
`_remapear_indices_de_producto` acaba de corregir); el guard se movió al lookup
de la caché, que es el único camino por el que `existing` puede ser un alta
encolada — el otro (`session.get` tras el resolver) sólo devuelve productos que
ya están en la base.

**Cancelar decía "importado correctamente".** `ColumnMapperPanel` llamaba al
mismo callback al confirmar y al cancelar. `onCancel` va separado y OBLIGATORIO:
un opcional con fallback a `onDone` deja al próximo caller repitiendo el bug.

**Los buckets a importar arrancaban en "ventas" fijo.** El bucket detectado se
agregaba encima, así que un archivo de productos llegaba con Productos Y Ventas
tildados (y en un summary legacy el backend honra los dos). Ahora la selección
se deriva de lo detectado, y si no se detectó nada no se tilda nada: mostrar la
ambigüedad es más honesto que resolverla eligiendo por el usuario.

**Dos endurecimientos sin cambio de comportamiento.** `_record_stock_movement`
mira `pending_balances` cuando no encuentra balance (un caller que pasara
`pending_balances` sin `balance_index` perdía la cantidad del primer movimiento
en silencio); la precarga de la identity map filtra por `tenant_id`.

Revisado sin hallazgos: el ordenamiento de `guarded_savepoint`, el fallback de a
uno tras `SavepointConflictError`, las huellas del camino batch (mismo hash del
ancla en las tres funciones, y la persistencia corre DESPUÉS del flush final del
lote), el orden altas → post-trabajo → balances, y que ninguna venta pueda
resolver contra un producto encolado.

Medición post-revisión, mismo archivo: **251 statements / 4,2 s** en frío y
**330 con las flags encendidas**, con el estado final idéntico.

## Abierto

- **El camino legacy** (summaries sin `mapping_contexts`) no chequea
  `_batch_productos.lleno`: mantiene todas las altas encoladas en memoria hasta
  el final. La cota del último commit cubre sólo el loop por contexto. Nombrado
  y no corregido: no hay forma de ejercitar ese camino con volumen en un test, y
  un punto de flush sin probar es peor que dejarlo dicho.
- **El timeout del proxy de Railway no está medido.** Los dos que sí: el cliente
  corta el confirm a los 16 min (`CONFIRM_TIMEOUT_MS`) y el lease del backend
  expira a los 15 (`DEFAULT_IMPORT_LEASE_TTL_SECONDS`) — los 18m33s excedían LOS
  DOS, así que ese import corrió con riesgo real de takeover a mitad de camino.
  Con 250 statements el confirm entra con margen bajo cualquiera de ellos, pero el
  techo del request sincrónico sigue existiendo para archivos mucho más grandes.
- **Re-importar agrega 26 movimientos** (`adjustment`) sobre el archivo de Asteria.
  Es preexistente —el baseline hace exactamente lo mismo— y quedó fuera de alcance.
- Los tres flags de rollout siguen en `[]` en producción.
