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
> **Estado (2026-10-07):** B·nuevo: PR #86 cerrado sin merge (`200ff590`, 9 hallazgos); reducido a paso 0 de evidencia (este PR, rama `fix/relectura-paso0-evidencia`: `diag_ediciones_en_riesgo.py`, `test_relectura_freno_escenarios.py` y el motivo del freno); automatización pendiente de decisión A/B/C del dueño. Plan vigente: [`bnuevo-reemplazo-seguro-pr86.md`](bnuevo-reemplazo-seguro-pr86.md). Lo que sigue abajo queda como diseño de referencia, no como alcance aprobado.

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

## Fase 2: costo, margen y capital en dinero

### 3. M·0: diagnóstico y ejemplos
- `scripts/diag_margen.py` es sólo SELECT y no imprime la URL. Revisa el «margen en 0» de ASTERIA: precio contra costo, unidad, fuente, marcas de envío e impuestos, régimen y la diferencia entre catálogo, Facts y dashboard.
- Antes de construir la pantalla te muestro **los ejemplos de aceptación (abajo) para aprobar**.

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
- **Una sola definición** compartida por la API, FactsService, `deterministic_finance` y el chat. Se corrige `calcular_margen_bruto`, que hoy resta todos los gastos.

### 6. M·3: dashboard, productos y ventas
- **Dashboard.** Un fact canónico en FactsService, «Margen bruto de ventas» del período, con un endpoint con schema Pydantic y los filtros comunes. Alimenta la tarjeta, la serie histórica y el desglose por categoría, **sin ninguna fórmula en React**. Se borra `buildMarginBreakdown`.
  - **Promedio:** `100 × Σ(ingreso comparable − costo) / Σ ingreso comparable`, sólo sobre ventas con costo y base confirmados.
  - **Cobertura por importe:** lo vendido que entra en el cálculo dividido por todo lo vendido, sobre el mismo universo.
  - **Lo que acompaña al número:** la ganancia conocida, el monto excluido y la metodología («costo de última compra»). «X de N productos con costo» es un dato secundario.
  - La **cobertura de costo no es completeness**. Se respeta la compuerta LOW que ya existe.
  - Un **umbral mínimo de cobertura del 50%** va como política propia, con tests de borde en 49,99 / 50 / 50,01. **Necesita tu OK.**
  - Un resultado parcial siempre dice su cobertura.
  - Quedan afuera las anuladas, las demo y el capital. Una venta mixta no se duplica por tener varios pagos.
- **Productos:** columna de margen estimado visible, con el motivo cuando no hay.
- **Ventas:** margen aplicado por operación, sólo para dueño o admin.
- **Permisos en el backend.** Al CASHIER no le llega el costo ni el margen por la API, ni siquiera en las respuestas en caché. No alcanza con ocultar la columna. Tampoco va nunca en el ticket.
- **Compra en «Otros»:** se resuelve con el mismo comando del contrato M·1. Se hace dentro de B·5.

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
- Decidir el umbral del 50%.
- Correr `diag_margen.py` y la consulta de D0.
- Autorizar el `--apply` de D·3.
- Los smokes en producción.
- B1 del POS.

## Primer paso al aprobar
1. Guardar este plan en `docs/plans/cierre-ingesta-capital-margen.md` y referenciarlo desde `cierre-pr48-y-b1.md`.
2. Pedirte el OK de la **fase 1** (B·3 + B·nuevo).
3. Recién entonces lanzar su workflow.
