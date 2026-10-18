# Plan v3: cierre de ingesta, capital y margen, con workflow por fases

## Contexto

Lo que queda pendiente de mi lado:
- **Del #48**: B·3, B·nuevo (preservar la edición sin depender de la posición) y B·4 a B·9.
- **De la parte D**: D·2 a D·7.
- Cerrar el #48.

**Pedido nuevo del dueño:** margen por producto calculado solo a partir de las compras, con o sin envío y con o sin impuestos, y su promedio en el dashboard.

Este plan consolida mi v2 con el plan consolidado que trajo el dueño. Tomo de ese plan lo que lo hace más correcto y dejo lo que agrega complejidad sin evidencia (el detalle está en la sección «Qué se tomó del plan aportado»).

### Estado verificado (2026-10-07)
- **Margen hoy.**
  - `ProductResponse.margin_pct` (`schemas/product.py:66`) devuelve 100% cuando el costo es 0.
  - El dashboard promedia en el navegador (`buildMarginBreakdown`, `features/dashboard/dashboardData.ts:205`): por categoría, sin ponderar y descartando en silencio. Eso rompe el invariante 2c.
  - `facts_service._cogs` (`facts_service.py:369`) usa el costo **actual**, no el que había al vender.
  - `deterministic_finance.calcular_margen_bruto` dice «bruto» pero resta todos los gastos.
- **Costo.**
  - `unit_cost_ars` es la última escritura (`ingestion_import_service.py:3080`), con el motor `domain/purchase_cost.py` y la decisión por hoja `purchase_cost_decision.py`, que va detrás de un rollout por tenant.
  - El envío está en `_vektor_costo_base`. `shipping_percentage` es texto que no se usa en ningún cálculo.
  - **El IVA no está modelado.**
  - El régimen (`domain/fiscal_condition.py`) hoy es sólo informativo.
- **ASTERIA** muestra el «margen en 0» sin diagnóstico.
- **En la rama del #48** ya están, como referencia y no como cherry-pick ciego:
  - B·3: `e9b9df3d`, `5d4eaccd`;
  - F-N: `a2fa53cc`;
  - F-B: `4a2844bf`;
  - F-D: `62e7c75b`, `e3ffc407`;
  - F-O.3: `c52b9806`;
  - cola: `a8de0c76`, `f17757eb`, `936e3c9d`.

### Decisiones del dueño (2026-10-07)
- El régimen interviene en el margen.
- El costo de referencia es el de la última compra.
- El dashboard muestra el promedio ponderado por ventas.
- Se mergea si da verde.
- No se corre la suite completa local en cada paso; sí al cerrar cada fase.
- Hay por lo menos tres fases (este plan tiene cuatro).

### Decisiones del dueño (2026-10-10), después del diagnóstico M·0
- **Costo manual frente a compra:** la edición manual vale hasta que llega una compra con fecha de negocio **posterior** a la edición. Una compra atrasada no pisa una edición más nueva.
- **Costo de catálogo sin compra:** origen `catalogo`, estimación rotulada. Una compra posterior lo reemplaza y no cuenta como compra confirmada.
- **El margen del health score usa la definición única de M·2** (se agrega a M·2).
- **Se agrega la reparación M·R** del stock inicial importado como compra (abajo). El `--apply` lo autoriza el dueño aparte.
- **El stock de apertura queda sólo como saldo inicial de inventario.** Contarlo además como aporte de capital en especie es opcional y va en D·6, con confirmación del dueño de que lo aportaron los socios.

### Decisiones del dueño (2026-10-11), al cerrar M·1
- **IVA en monotributo e informal:** se trata como no recuperable por régimen, sin confirmarlo compra por compra (así quedó en `base_comparable` de M·1d).
- **Envío desconocido:** se mantiene estricto. Un costo con envío `desconocido` no da margen confirmado: sale como estimación con su motivo hasta que la compra pida envío e IVA (M·3 / B·5). No se toma «desconocido» como «no hubo envío» en ninguna ruta.

## Contrato de ejecución (vale para todos los pasos)

- **Autorización por fase.** Antes de cada fase muestro su lista de pasos con lo que cambia y cómo se acepta. Tu OK de la fase cubre sus pasos y sus merges. Si un paso cambia de alcance o necesita una decisión nueva, el workflow se detiene y te pregunta.
- **Punto de partida.** Cada paso parte de `main` actualizado y verifica qué ya está integrado. Los commits del #48 son referencia. Se lleva una **matriz de portado** en `docs/plans/cierre-pr48-y-b1.md` (paso → PR → commit → resultado).
- **Agentes.** Hay como máximo **4 activos**, contando al coordinador. Para revisar se liberan los slots: el coordinador más 3 revisores.
- **Merge sobre el SHA revisado.** Lo que se mergea es exactamente el SHA completo que se revisó y que tiene los checks en verde, con `gh pr merge --squash --match-head-commit <sha completo>`. Después se sincroniza `main`.
  - Si hubo rebase o push, el SHA cambia: hay que esperar checks nuevos y revisar sólo el diff nuevo.
  - Que el CI esté verde no reemplaza la revisión.
- **CI en rojo.**
  - Primero se distingue si es una regresión, configuración o infraestructura.
  - Si a la segunda vuelta sigue rojo, el workflow se detiene y te paso un informe del bloqueo con la acción siguiente.
  - No se relanza esperando un verde de casualidad.
  - Hay que exigir que corran los jobs que corresponden, no aceptar un verde de un job ajeno o salteado por `paths`.
- **Reglas de siempre:**
  - nada de `ruff format` / `make format` ni `git add -A`;
  - la doc del dominio va en el mismo commit;
  - `today_ar()` en los tests;
  - migraciones aditivas e idempotentes colgadas del head real;
  - cada comando valida el tenant del JWT y los permisos;
  - los contratos toleran que front y back se desplieguen en cualquier orden;
  - trailer `Co-Authored-By: claude-flow <ruv@ruv.net>` y la línea de claude-flow en el PR, según la política del repo.
- **Producción.** No afirmo nada de producción sin evidencia. Lo que no puedo ejecutar te lo paso como script o consulta concreta, sin secretos.

## Fase 1: cerrar la pérdida de datos (B·3 y B·nuevo)

### 1. B·3: categoría y códigos
- **Categoría.** `build_incomplete_product` (`ingestion_import_service.py:2822`) carga el rubro (`_load_tenant_vertical`) y llama a `infer_category`.
  - Asigna la categoría **sólo si está vacía y la confianza es alta**. Con confianza media no la asigna; si existe un lugar donde mostrar sugerencias, se sugiere ahí. Con baja no se inventa nada.
  - Respeta las ediciones del usuario y `requires_completion`.
- **Código externo al fusionar.** `product_dedup_service.py:1116`:
  - Si el ganador no tiene código, recibe el código y la fuente del descartado. Para no chocar con el índice parcial `uq_products_tenant_external_code`, primero se libera el del descartado y todo ocurre en la misma transacción.
  - Si los dos tienen, gana el del ganador, y el del descartado queda en `after_json` de `MERGE_PRODUCT` como conflicto.
  - Incluye snapshot y deshacer. No toca stock ni precio.
- **Tests:**
  - inferencia alta y ambigua;
  - código presente, ausente y en conflicto;
  - deshacer después de una modificación posterior;
  - rollback;
  - **`-m postgres` antes del merge**, por unicidad y concurrencia.

### 2b. B·3b: «+ Nueva categoría» en `/products` — HECHO
- Sólo frontend, sin endpoint nuevo: el editor de producto crea la categoría con `POST /products/custom-categories` (OWNER/ADMIN), la deja seleccionada e invalida el catálogo. «Otra» junto a la sugerida abre el mismo editor. Rama `feat/productos-nueva-categoria`, PR propio, separado de B·nuevo.

### 2. B·nuevo: preservar la edición sin depender de la posición
> **Estado (2026-10-08):** B·nuevo: PR #86 cerrado sin merge (`200ff590`, 9 hallazgos); reducido a paso 0 de evidencia (#88: `diag_ediciones_en_riesgo.py`, `test_relectura_freno_escenarios.py` y el motivo del freno). **Decisión A** del dueño con el diagnóstico de producción (0 registros editados de archivos importados): automatización descartada, limitación documentada. Los tres defectos del freno que mostró el paso 0 se arreglaron igual: (c) #89, (b) #90 + #91, (a) #92 + #94. Plan vigente: [`bnuevo-reemplazo-seguro-pr86.md`](bnuevo-reemplazo-seguro-pr86.md). Lo que sigue abajo queda como diseño de referencia, no como alcance aprobado.

Primero se diseña la correspondencia (con revisión), y recién después se toca `_split_records` / `_reconcile` (`reread_service.py:766`, `:1213`).
- **Tres cosas separadas:** el identificador lógico, los **datos originales importados** (la edición manual no los modifica, y son los que se usan para reconciliar) y el localizador hoja/fila.
- **Con clave de operación.** `clave_de_operacion` (`domain/operation_identity.py:333`) identifica la operación. Un comprobante de varias líneas además necesita un discriminador de línea. El `external_code` de un producto, cliente o proveedor **no** identifica una venta.
- **Sin ID de origen.** La correspondencia es uno a uno sobre la evidencia original más el contexto estable. **Si es ambigua, el apply se bloquea** y pide resolución explícita, sin tocar datos. Nunca se copia una edición a otra operación, nunca se conserva una edición creando su duplicado y nunca se saltea otra fila para que cierren los conteos.
- **Alcance de la revisión:** huellas de importación, exclusiones, filas que se reclasificaron desde «Otros», stock, medición de pérdidas y F11. La compuerta del #81 sigue como red.
- **Datos viejos (legacy).** Se completan metadatos sólo donde la correspondencia se puede demostrar. El resto conserva su edición y queda para revisión.
- **Tests:**
  - filas ordenadas distinto, insertadas y eliminadas;
  - filas gemelas;
  - comprobante de varias líneas;
  - el mismo código en varias ventas;
  - un contexto editado entero;
  - cambio de parser;
  - dos relecturas seguidas;
  - clasificación desde «Otros»;
  - stock y reversión;
  - sin aceptar la pérdida, rollback completo;
  - **`-m postgres`**.

**Cierre de la fase 1:**
- suite backend completa con el env de CI;
- `-m postgres -n 0` contra `vektor_pgtest`, confirmando que corrieron y no quedaron salteados por falta de DSN;
- jest `--runInBand`;
- un ensayo con un fixture tipo ASTERIA con filas corridas: no duplica la edición y no saltea ninguna fila.

Se registran el SHA, los comandos, las cantidades y los skips.

#### Registro del cierre de la fase 1 (2026-10-08) — CERRADA

SHA: `main` `4abdd0d4` (#94), desplegado en Railway y Vercel (deployment statuses `success`). Entraron: B·3, B·3b (#87), paso 0 de B·nuevo (#88) y los defectos c/b/a (#89, #90, #91, #92, #94).

| Prueba | Comando | Resultado |
|---|---|---|
| Backend completo | `pytest --cov-fail-under=60` con el bloque `env:` de `ci-backend.yml` | 5.847 passed, 1 failed, 201 skipped; cobertura 80,14% |
| Postgres | `pytest -m postgres -n 0 -p no:randomly` con `TEST_PG_DSN`, `DATABASE_URL` y `DATABASE_URL_SYNC` en `localhost:5432/vektor_pgtest` (alembic en head `20261002_0001`) | 201 passed, 0 skipped, 0 failed |
| Frontend | `npx jest --runInBand` | 913 passed, 3 failed de 916 (101 de 102 suites) |
| Ensayo tipo ASTERIA | `pytest app/tests/integration/test_relectura_freno_escenarios.py` con `TEST_PG_DSN` | 28 passed, 0 xfail, 0 skip |

- **Skips:** los 201 del backend son los `-m postgres` de la corrida SQLite (sin `TEST_PG_DSN`, por diseño) y corrieron todos en la segunda fila. No queda ningún `xfail` en `app/tests`. Hay skips condicionales que no se dispararon: el schema sin migrar en 4 tests `_pg`, `test_auto_custom_field_proposal` y `test_product_scan:275`.
- **Ensayo:** en cada escenario de filas corridas (fila insertada antes de la edición, gemelas, compra con stock, corrimiento en el mismo día, fila nueva en la posición de la edición) el resultado es correcto, o frena con rollback, y `_releer` exige que la foto completa quede idéntica: ids, importes, `source_row_ref`, huellas, stock, movimientos y vínculos.
- **Falla del backend, ambiental y preexistente:** `test_limites_todos_los_caminos.py::test_el_fallback_sincronico_deja_el_archivo_rechazado_no_fallado` necesita el broker de Celery caído. En local, con Redis arriba, el archivo queda `PENDING`. Con `CELERY_BROKER_URL=redis://localhost:6390/1` (inalcanzable, como en CI, que no tiene Redis) pasa. Viene de E6c-2 (`65cc7585`), y el CI backend dio verde en `4abdd0d4`. Deuda: forzar el broker caído dentro del test.
- **Fallas de jest, preexistentes:** 3 tests de `src/features/pos/__tests__/pos_screen.test.tsx`, por estado que se filtra del test anterior. El archivo solo pasa 21 de 21. La fase 1 no tocó el frontend del POS (último commit ahí: #83). Deuda: limpiar el estado entre tests.

## Fase 2: costo, margen y capital en dinero

### 3. M·0: diagnóstico y ejemplos
- `scripts/diag_margen.py` es sólo SELECT y no imprime la URL. Revisa el «margen en 0» de ASTERIA: precio contra costo, unidad, fuente, marcas de envío e impuestos, régimen y la diferencia entre catálogo, Facts y dashboard.
- Antes de construir la pantalla te muestro **los ejemplos de aceptación (abajo) para aprobar**.
- **Resultado en producción (2026-10-10), ASTERIA:**
  - el subscore de margen del health score es 0 porque el 100% de la mercadería de la ventana de 90 días ($7.538.005) son gastos «Stock inicial: …»: el catálogo importado como **compra** (`purchase` + `catalog_initial_stock`, con COGS). El health score lo resta como inventario del mes contra $323.500 de ventas y da −776%;
  - sólo el 9% de lo vendido tiene costo: FactsService toma el resto como costo 0 y muestra 96,2% (lo corrige la cobertura mínima de M·3);
  - 375 de 380 productos con costo lo tienen sólo del catálogo; 5 tienen costo mayor que el precio; 8 empates de fecha con costos distintos;
  - la cuenta deco de prueba de producción está vacía y no sirve de contraste: el contraste se hace con el demo deco en la base local.

### 4. M·1: el contrato de costo de compra
- **«Última compra»** es la última compra confirmada y válida **por fecha de negocio**, no la última fila procesada.
  - El desempate es estable y se documenta.
  - Si se anula o se revierte la compra elegida, se vuelve a elegir otra.
  - Se mantienen la edición manual y la protección actual de con flete / sin flete.
  - Precedencia cuando no hay compra: un costo manual confirmado o el stock inicial o aportado. Cada uno conserva su origen.
- **Contrato del costo de una compra (versionado)**, en los metadatos de la compra. Antes de decidir si va en columnas (migración aditiva) o en `custom_fields` con claves reservadas y validadas, hay que ver dónde vive.
  - **Envío:** `incluido` | `adicional` | `no_aplica` | `desconocido`.
  - **Impuesto:** `incluido` | `adicional` | `no_aplica` | `desconocido`, con tasa e importe cuando se conocen.
  - **Procedencia:** fuente, fecha y comprobante.
  - Los datos viejos se aceptan **sin marcarlos confirmados**.
- `monto_incluye` (la decisión de la hoja) dice que los ajustes ya están adentro. **No prueba que haya IVA**: la decisión del IVA es aparte.
- El envío **se calcula al registrar la compra**, con el motor `purchase_cost.py` y sobre ese mismo comprobante, nunca al consultar el producto.
  - Un envío compartido se reparte al centavo dentro de su comprobante.
  - `shipping_percentage` es evidencia informativa, no un dato para sumar.
  - Si el envío ya está dentro del costo, no se resta otra vez del resultado.
- **IVA:**
  - el régimen guía la comparación;
  - la tasa y si es recuperable se **confirman por operación**;
  - el 21% se puede sugerir, pero **nunca queda como dato confirmado por defecto**;
  - `no_aplica` ≠ `adicional`;
  - un **Responsable Inscripto** con IVA recuperable confirmado compara **neto contra neto**;
  - en los demás casos, el IVA no recuperable forma parte del costo cuando corresponde;
  - un IVA ya incluido se extrae por importe o por base y tasa confirmados, nunca dos veces;
  - el alias legacy `registered` no habilita un cálculo fiscal confirmado.
- **Sin base comparable** hay un motivo, una estimación separada y queda afuera del margen confirmado. Un costo 0 de un producto incompleto queda pendiente.
- `docs/claude/plataforma.md`: el régimen pasa de «sólo informativo» a «guía el margen» y no se usa para nada más.

### 5. M·2: margen por producto y por venta
- **Catálogo:** margen **estimado** con el precio vigente y el costo de referencia. El contexto del régimen se arma en el servicio. La propiedad de Pydantic no consulta nada ni deduce el tenant.
- **Venta: el costo se congela por línea al confirmar.** Es la fuente elegible a la fecha de la venta. Una compra posterior no lo cambia y las ventas pasadas no se revalúan.
  - Es una migración aditiva en la línea de venta. Primero hay que verificar si es `sales_entries` o `pos_operation_lines`, o las dos.
  - Para lo histórico, se usan movimientos sólo si son una fuente válida y quedan auditados. Si no, la estimación se etiqueta como tal, nunca como «realizado».
- **POS y ventas sin red: el snapshot del costo lo calcula el SERVIDOR** con la fecha de negocio de la venta al sincronizarla.
  - El navegador no manda costo y el cajero no lo ve.
  - Si se perdió la respuesta, el reintento idempotente devuelve la misma venta con el mismo snapshot.
  - Si falta evidencia, la venta entra con el margen pendiente.
- **Fórmulas:**
  - `ganancia = ingreso comparable después del descuento − costo vendido`;
  - `margen % = ganancia / ingreso × 100`, sólo si el ingreso es mayor que 0.
  - El margen negativo es válido. No es recargo sobre costo.
  - En la venta por peso, la cantidad se normaliza a la unidad de venta.
- **Una sola definición** compartida por la API, FactsService, `deterministic_finance`, el chat **y el subscore de margen del health score**. Se corrige `calcular_margen_bruto`, que hoy resta todos los gastos, y el health score deja de calcular `(ventas − inventario − fijos) / ventas`: comprar mercadería no es costo de lo vendido (agregado 2026-10-10 por el diagnóstico M·0).

### 6. M·3: dashboard, productos y ventas
> **Replanteado el 2026-10-16** (decisiones del dueño, plan en `docs/plans/m3-margen-neto.md`): el indicador de salud es el **margen neto** del **mes cerrado** = (ingreso − costo vendido − todo gasto no-mercadería, impuestos incluidos) / ingreso; mientras corre el mes se ve el **bruto del mes en curso**. Tarjeta, chat, alertas y subscore leen el mismo hecho. Los impuestos llevan tipo (el IVA de un RI no resta). Cuatro PRs: M·3-1 (neto, backend), M·3-2 (pantalla), M·3-3 (tipo de impuesto), M·3-4 (antigüedad del costo). Lo que sigue abajo es el texto original; donde choca, manda el plan nuevo (p. ej. «sólo ventas con costo confirmado»: rige la definición de M·2b, confirmado/estimado/ausente).

- **Dashboard.** Un fact canónico en FactsService, «Margen bruto de ventas» del período, con un endpoint con schema Pydantic y los filtros comunes. Alimenta la tarjeta, la serie histórica y el desglose por categoría, **sin ninguna fórmula en React**. Se borra `buildMarginBreakdown`.
  - **Promedio:** `100 × Σ(ingreso comparable − costo) / Σ ingreso comparable`, sólo sobre ventas con costo y base confirmados.
  - **Cobertura por importe:** lo vendido que entra en el cálculo dividido por todo lo vendido, sobre el mismo universo.
  - **Lo que acompaña al número:** la ganancia conocida, el monto excluido y la metodología («costo de última compra»). «X de N productos con costo» es un dato secundario.
  - La **cobertura de costo no es completeness**. Se respeta la compuerta LOW que ya existe.
  - Un **umbral mínimo de cobertura del 50%** va como política propia, con tests de borde en 49,99 / 50 / 50,01. **Aprobado por el dueño el 2026-10-09.** Es cobertura de DATOS (importe vendido con costo cargado sobre el total vendido), no un piso del margen: el margen se muestra sea cual sea su valor, incluso negativo.
  - Un resultado parcial siempre dice su cobertura.
  - Quedan afuera las anuladas, las demo y el capital. Una venta mixta no se duplica por tener varios pagos.
- **Productos:** columna de margen estimado visible, con el motivo cuando no hay.
- **Antigüedad del costo (agregado 2026-10-14, por la inflación).** El costo de reposición es el de la última compra, no el precio de hoy: con ~4% mensual, una compra de hace 3 meses deja el costo ~12% atrasado y el margen inflado sin avisar.
  - Cada venta con costo lleva la fecha de la compra de la que sale. Si esa compra tiene más de **45 días** a la fecha de la venta (umbral como política propia, con tests de borde en 44/45/46), la venta entra al margen rotulada «costo desactualizado».
  - El fact del margen informa, junto a la cobertura, el **importe vendido con costo desactualizado** y su porcentaje. La tarjeta y la columna de productos dicen «última compra hace X días» cuando aplica.
  - Es un dato medido: **no se ajusta ningún costo por inflación** (no-invention). El margen se muestra igual, con el rótulo.
- **Ventas:** margen aplicado por operación, sólo para dueño o admin.
- **Permisos en el backend.** Al CASHIER no le llega el costo ni el margen por la API, ni siquiera en las respuestas en caché. No alcanza con ocultar la columna. Tampoco va nunca en el ticket.
- **Compra en «Otros»:** se resuelve con el mismo comando del contrato M·1. Se hace dentro de B·5.

### 6b. M·R: reparar el stock inicial importado como compra (agregado 2026-10-10)
- **Qué repara:** los gastos «Stock inicial: …» (`expense_entries` COGS/INVENTORY) y sus movimientos `purchase` + `catalog_initial_stock` que un import de catálogo registró como compra del período. Pasan a ser el **saldo de apertura**: `adjustment` + `catalog_initial_stock`, con su costo unitario y sin gasto ni salida de dinero.
- El stock y el costo unitario de cada producto no cambian; al venderse, ese costo entra en el costo de lo vendido como cualquier otro.
- **Script fuera de Alembic**, con la mecánica de D·3:
  - dry-run por tenant que lista cuántos gastos y movimientos toca y el efecto en el margen y el score;
  - guarda el vínculo con el gasto y el movimiento originales, se puede reejecutar sin duplicar y es reversible;
  - **el `--apply` lo autoriza el dueño aparte.**
- **Hacia adelante:** el import de catálogo deja de registrar la existencia inicial como compra salvo que la hoja la declare compra del período explícitamente. Se verifica primero qué decisión de la hoja la generó en ASTERIA.
- **Aceptación:** en ASTERIA, después del `--apply`, la mercadería de la ventana deja de incluir los $7,5 M y el subscore de margen sale de la definición de M·2, con su cobertura.
- **Cómo quedó (2026-10-12, `scripts/reparar_stock_inicial_como_compra.py`):** se anula **sólo el gasto**; el movimiento sigue `purchase` + `catalog_initial_stock`. Decisión del dueño, después de dos revisiones: convertirlo a `adjustment` vaciaba Proveedores (lee sólo `purchase`), cambiaba la precedencia del costo de referencia en el próximo recálculo de M·1c y movía el ancla de la deduplicación. El margen, el flujo, la mercadería y la caja se corrigen igual porque leen gastos. La elección guardada vuelve a «apertura» en `parsed_summary_json` y en todas las decisiones recordadas «compra» del tenant (`ingestion_schema_decisions`), para que una relectura no recree los gastos. «Compras por proveedor» sigue contando esa mercadería, porque el movimiento sigue siendo compra. Los archivos con una relectura aplicada o abierta no se reparan (se informan). Sin migración. **Hacia adelante:** el import ya trae «ya la tenía» por defecto y pregunta por hoja; no se cambia salvo que el dry-run de ASTERIA muestre otro camino. Detalle en `docs/claude/plataforma.md`.

### 6c. M·P4b: el costo promedio neto de IVA para un RI (agregado 2026-10-16)
- **Qué falta:** el costo promedio (`product_cost_history`) promedia el costo CRUDO de la compra (`costo_promedio_repository`), mientras el costo de reposición de la venta pasa por `base_comparable` y para un Responsable Inscripto llega neto de IVA. Compararlos daría una brecha falsa del tamaño del IVA.
- **Mientras tanto (M·P4-2):** para un RI, `margen_promedio_vs_reposicion` sale ausente con el motivo «costo promedio todavía sin neto de IVA» y el chat no dice nada. La columna de Productos (M·P4-1) no lo excluye: allí los dos costos están en base cruda.
- **El paso:** llevar el promedio a la misma base que la reposición de la venta (`base_comparable` por régimen) y recalcular el historial con una publicación completa. Recién entonces se levanta la exclusión. Plan aparte, después de M·3.
- **POSTERGADA (2026-10-17, decisión del dueño), hasta B·5.** La revisión mostró que ningún escritor confirma `tax_recoverable`: con la opción A un RI quedaría con todo su historial sin medir y la comparación ausente igual. El código queda en un PR en borrador; se retoma cuando B·5 confirme envío e IVA por compra. Faltará además marcar pendiente el historial de los RI en el mismo deploy y decidir qué pasa con el snapshot de las ventas al cambiar de régimen.
- **Lo que tiene la rama (opción A del dueño):** un solo promedio, en la base comparable de cada compra (`domain/costo_promedio.costo_comparable`). Un RI sin neto confirmado deja esas unidades sin costo comparable (`incompleto`, o `sin_medicion` si no queda ninguna con costo): nunca mezcla neto con IVA. La comparación de M·P4-2 se habilita para RI sólo sobre ventas con reposición confirmada. En Productos el promedio de un RI se rotula «sin IVA» (`avg_cost_sin_iva`). Un cambio de régimen recalcula todo el tenant; uno de contrato, el producto desde su día. Sin migración; el historial publicado se corrige con `backfill_costo_promedio.py --all --apply`. Detalle en `docs/claude/plataforma.md`.

### 7–10. D·2 a D·5: capital en dinero
7. **D·2**: tabla `owner_capital_movements`.
   - **Tipos:** aporte, retiro, préstamo de socio o devolución.
   - **Forma:** dinero o especie.
   - **Datos:** socio opcional, fecha de negocio, medio de pago, importe positivo, procedencia y anulación.
   - Comandos auditados con PIN y tenant, y carga manual.
   - **Las escrituras nuevas se cambian antes de reparar las viejas.** `/expenses/profit-withdrawal` (`expenses.py:236`) sigue con la misma respuesta, para que un frontend viejo no se rompa.
8. **D·3**: reparación de los retiros existentes, fuera de Alembic.
   - Dry-run por tenant y período, sólo `profit_withdrawal`.
   - Guarda el vínculo al gasto original, se puede reejecutar sin duplicar y es reversible.
   - Concilia gastos, margen y score.
   - **El `--apply` lo autorizás vos aparte.**
9. **D·4**:
   - aportes y retiros por socio, en dinero y en especie, más los préstamos pendientes;
   - el flujo operativo va **separado** del flujo con capital y de la caja contada;
   - no se llama «balance contable».
10. **D·5**: capital inicial opcional en el onboarding.
    - Es declarado (`PESO_DECLARADO`), con fecha y sin duplicarse si se repite el onboarding.
    - No se confunde con la caja actual.

### Aceptación de la fase 2
- **Margen ponderado:** ventas de $100 con costo $80 y de $900 con costo $450 → ganancia $470, **47%**. El promedio simple daría 35%.
- **Costo con envío e impuesto:** 10 unidades, mercadería $1.000 + envío $100 + impuesto no recuperable confirmado $210 → **$131 por unidad**. Si ya venían incluidos, no se vuelven a sumar.
- **Responsable Inscripto:** venta $1.210 y costo $605, los dos con IVA 21% recuperable confirmado → $1.000 contra $500, **50%**.
- **Otros casos de margen:**
  - impuesto `no_aplica`;
  - base desconocida, que queda afuera con su motivo;
  - una compra posterior a la venta, que no cambia el snapshot;
  - una venta sin red: sincronizada después de una compra nueva, conserva la fuente elegible a su fecha. Con la respuesta perdida y un reintento queda una sola venta, un solo snapshot y ningún costo visible para el cajero;
  - anulación y relectura.
- **Capital:** aporte $100.000 y retiro $10.000 → flujo de capital $90.000, sin ventas ni gastos nuevos y sin cambio en el margen bruto. La reparación de un retiro viejo saca su efecto sobre el resultado y el score una sola vez.
- **Conciliación:** el detalle, la tabla, la serie y la tarjeta muestran el mismo número.

**Cierre de la fase 2:** suites completas, `-m postgres` (hay migraciones) y un ensayo conjunto del margen y el capital en demo.

## Fase 3: B·4 a B·9, calidad, resolución y alias
11. **B·4**:
    - F-N revisado: completa sólo lo vacío y respeta `has_user_edits`;
    - una fila Total/Subtotal sólo con la etiqueta exacta y evidencia de que es una agregación;
    - «Total Clean» queda como test.
12. **B·5**:
    - `/others/summary` agrupado por archivo × motivo, con conteos del grupo completo;
    - descarte con vista previa y confirmación;
    - la resolución de una compra usa el contrato de M·1.
13. **B·6, F-D**:
    - completa sólo campos vacíos, una vez por entidad;
    - revierte con `UPDATE_*` si eso alcanza para revertir completo, y eso se verifica antes de descartar la migración;
    - ledger y F11.
14. **B·7**: cliente o proveedor por código normalizado y único en el tenant. Un código desconocido o en conflicto no crea un maestro ni elige uno al azar.
15. **B·8a**: tabla `product_aliases` con procedencia.
    - Se busca después del código de barras y el SKU, y nunca en el escaneo del POS.
    - Un alias ambiguo no vincula.
    - Vincular es auditado, no mueve stock y actualiza el margen.
    - Al fusionar productos, sus alias pasan al ganador sin pisar los de otro producto.
    - F11 no borra lo que se aprendió a mano.
16. **B·8b**: la pantalla, con resolución de a una, conflictos y costo o base pendientes. Nada de asignaciones masivas silenciosas.
17. **B·9**: sólo «Guardar como campo propio» e «Ignorar vacías». No hay «Aceptar ambiguas» ni nada que saltee `user_selected` o la compuerta de riesgo.

**Cierre de la fase 3:** suites completas y `-m postgres`. Smoke de import con totales, varias hojas, «Otros», campos cruzados, código y alias: comparar los datos antes y después, y probar deshacer.

## Fase 4: D·6, D·7 y cierre
18. **D·6, mercadería aportada**: el stock de apertura y el aporte valorizado van atómicos.
    - Sin gasto ni salida de dinero. Al venderse entra en el costo vendido.
    - El equipamiento es informativo y no se amortiza.
    - Hay una política definida para revertir cuando ese stock ya se vendió.
    - Opcional: valorizar como aporte en especie un stock de apertura ya reparado por M·R (por ejemplo ASTERIA), sólo si el dueño confirma que lo aportaron los socios.
19. **D·7**: destinos importables en el libro mixto, el Libro Diario y en hojas sueltas.
    - Con la identidad de la fase 1, procedencia, ledger y F11.
    - Un préstamo bancario o sin socio identificado sigue en «Otros».
    - Releer no duplica ni pisa ediciones.
20. **Cerrar el #48**:
    - matriz de lo portado, lo descartado y en qué PR;
    - se borra la rama sólo con tu autorización y sin commits útiles pendientes;
    - `spike/pos-b1` y `.venv312` quedan afuera.

**Cierre de la fase 4:**
- suites completas y `-m postgres`;
- ensayo: aportar mercadería, vender una parte y anular o releer;
- un **agente de completitud** contrasta el alcance contra la matriz, los tests y lo pendiente.

## Workflow (una corrida de `Workflow` por fase, después de tu OK de esa fase)

Por cada paso, en serie, como `pipeline`:
1. **Implementar** (un agente, o dos si el paso tiene backend y frontend, en archivos distintos y con el contrato acordado):
   - rama desde `main`;
   - tests puntuales con mutación sobre la protección crítica;
   - ruff, mypy, tsc y lint de lo tocado;
   - `-m postgres` cuando el paso toca identidad, unicidad, locks, migraciones o reversión;
   - `git diff --check`, push y PR.
2. **Revisar** (3 agentes en paralelo sobre el SHA final), cada uno con una lente:
   - **integridad de datos**;
   - **invariantes**: tenant, FactsService, no-invention, permisos, deploy en paralelo e idempotencia;
   - **tests**: ¿fallan sin el arreglo?, ¿cubren el caso real?
3. **Verificar**: se consolidan los hallazgos, y verificadores adversariales los atacan en tandas de 3. Un riesgo material no se descarta sólo porque no se reprodujo rápido.
4. **Corregir** y volver a revisar **sólo el diff nuevo**.
5. **Merge**: con el CI verde sobre ese SHA, `--match-head-commit`, y después se sincroniza `main`. Si el CI está rojo, aplica la regla de dos vueltas del contrato.

Al cierre de la fase, un agente corre las suites completas y deja el registro. **Si el cierre falla, no arranca la fase siguiente.** El arreglo va en un PR revisado.

## Qué se tomó del plan aportado y qué no
- **Se tomó:**
  - el orden por riesgo: la pérdida de datos primero, y la identidad resuelta antes de la ingesta de aportes;
  - en la identidad, la ambigüedad bloquea en lugar de usar contenido + ordinal;
  - IVA y envío confirmados por operación, sin 21% por defecto y con `no_aplica` ≠ `adicional`;
  - el envío se calcula al registrar la compra;
  - la última compra por fecha de negocio, con desempate y reselección;
  - el costo congelado por venta;
  - cobertura por importe en lugar de por productos;
  - el umbral como política aparte;
  - permisos del cajero en el backend;
  - corregir `calcular_margen_bruto`;
  - los ejemplos de aceptación;
  - la compatibilidad de `profit-withdrawal`;
  - el merge sobre el SHA revisado;
  - la regla de dos vueltas;
  - el máximo de 4 agentes;
  - la matriz de portado.
- **Se ajustó:**
  - **Costo en la venta sin red:** el plan aportado pedía que el navegador guardara un snapshot del costo con versión, y que el servidor lo validara. Acá lo calcula el servidor al sincronizar, con la fecha de la venta. Cumple lo mismo (la compra posterior no es elegible, el cajero no ve el costo, el reintento es idempotente) sin mandar costos al navegador ni validar datos del cliente.
  - **«Mini-plan con OK antes de cada paso»:** pasa a OK por fase, según tu instrucción de mergear con verde.

## Lo que queda de tu lado
- Aprobar cada fase.
- Aprobar los ejemplos de M·0.
- ~~Decidir el umbral del 50%~~ (aprobado 2026-10-09).
- ~~Correr `diag_margen.py`~~ (corrido 2026-10-10) y la consulta de D0.
- Autorizar el `--apply` de D·3 y el de M·R.
- Los smokes en producción.
- B1 del POS.

## Primer paso al aprobar
1. Guardar este plan en `docs/plans/cierre-ingesta-capital-margen.md` y referenciarlo desde `cierre-pr48-y-b1.md`.
2. Pedirte el OK de la **fase 1** (B·3 + B·nuevo).
3. Recién entonces lanzar su workflow.
