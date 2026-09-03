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

Se reemplaza por el MISMO `ColumnMapperPanel`. Los defaults se **transfieren**, no
se reemplazan: el bucket del tipo detectado arranca tildado y a la vista (pasar de
"los cinco en true" a "sólo ventas" habría cambiado en silencio QUÉ se importa).

## Compuertas nuevas

- **Presupuesto ABSOLUTO de statements por forma de SQL**, no un ratio: un ratio
  sólo detecta crecimiento superlineal y un N+1 LINEAL de ocho queries por fila lo
  pasa sin despeinarse. El ratio queda como red secundaria contra un O(n²).
- **El camino de excepción del lote**: no pierde el resto, no fusiona identidades
  distintas, distingue qué restricción falló, y no deja transients huérfanos.
- Test de `FileUploadSection` — no existía ninguno, y ése es el hueco por el que
  entró el bug.

## Abierto

- **El timeout del proxy de Railway no está medido.** Los dos que sí: el cliente
  corta el confirm a los 16 min (`CONFIRM_TIMEOUT_MS`) y el lease del backend
  expira a los 15 (`DEFAULT_IMPORT_LEASE_TTL_SECONDS`) — los 18m33s excedían LOS
  DOS, así que ese import corrió con riesgo real de takeover a mitad de camino.
  Con 250 statements el confirm entra con margen bajo cualquiera de ellos, pero el
  techo del request sincrónico sigue existiendo para archivos mucho más grandes.
- **Re-importar agrega 26 movimientos** (`adjustment`) sobre el archivo de Asteria.
  Es preexistente —el baseline hace exactamente lo mismo— y quedó fuera de alcance.
- Los tres flags de rollout siguen en `[]` en producción.
