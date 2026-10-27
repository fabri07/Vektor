# Seguimiento consolidado de hallazgos

Estado de cada hallazgo de la revisión profunda (`etapa-0-1.md`, `etapa-2.md`) y de las revisiones de PR (`hallazgos-de-revisiones-de-pr.md`). Los informes no se editan; el estado vive acá y se actualiza en el mismo momento en que se mergea el arreglo.

Estados: **pendiente** · **parcial** (el PR cerró una parte; la nota dice qué falta) · **mergeado** (en `main` con CI verde sobre el head; el deploy pasó, pero eso NO prueba que producción quedó corregida: los datos históricos no se repararon) · **verificado en prod** (comportamiento comprobado en producción, o datos reparados) · **postergado** (decisión del dueño).

| Id | Sev. | Veredicto | Tema | Hallazgo | Estado | PR / nota |
|---|---|---|---|---|---|---|
| B-1 | critico | confirmado | T1 | Los cobros de fiado («inflow») suman como ingreso: el Resumen económico cuenta dos veces cada venta fiada que se cobra | mergeado | #174 |
| D-1 | critico | confirmado | T1 | El resumen económico cuenta dos veces cada venta fiada que después se cobra | mergeado | #174 |
| B-2 | grave | confirmado | T1 | «Cuentas por cobrar» del chat suma todo lo fiado y no resta los cobros | mergeado | #174 |
| D-2 | grave | confirmado | T1 | Las ventas del BSL incluyen cobros de fiado y eso infla crecimiento, completitud y la mezcla con lo declarado | mergeado | #174 |
| D-3 | grave | confirmado | T1 | La proyección de caja cuenta como ingreso de caja el fiado, la tarjeta de crédito y además los cobros de fiado | parcial | #174 sacó el fiado y sus cobros; la tarjeta de crédito sigue (R-10) |
| A-1 | grave | confirmado | T2 | Anular un ticket pisa el stock de una venta concurrente (lost update en void_movement) | mergeado | #180 |
| B-4 | grave | confirmado | T2 | Anular o editar una venta repone stock sin bloquear la fila del producto: se pierde una venta concurrente | mergeado | #180 |
| C-1 | grave | confirmado | T2 | void_movement / unvoid_movement / register_stock_loss mutan stock_units sin lock de fila: se pierde el descuento de una venta concurrente | mergeado | #180 |
| A-2 | grave | confirmado | T2 | Resolver una diferencia con ajuste usa un stock viejo y pisa ventas concurrentes | mergeado | #180 |
| B-5 | grave | confirmado | T2 | La compra manual suma stock sobre una instancia vieja del producto: pisa ventas concurrentes | mergeado | #180 |
| C-3 | grave | confirmado | T2 | El replay de inventario descuenta sobre Product sin lock y con el stock posiblemente viejo del identity map: pisa ventas en vivo y el clamp a 0 deja el ledger desalineado | mergeado | #181 |
| E0-B-3 | grave | e0 | T2 | Aplicar ventas históricas (decrement_stock_bulk) pisa stock_units con un valor absoluto calculado desde un saldo leído sin lock: se pierden ventas en vivo concurrentes y el clamp esconde la sobreventa | mergeado | #181 |
| B-3 | grave | confirmado | T3 | Anular o editar el gasto de una compra deja vivo el movimiento de stock: stock, costo y respaldo quedan como si la compra existiera | PR abierto | #192 (sólo bloqueo; anular compra entera después del 30/10) |
| C-4 | grave | confirmado | T3 | Editar producto o cantidad de una venta importada cuyo descuento ya se aplicó (replay) no revierte ni rehace el movimiento: stock mal en los dos productos | PR abierto | #190 |
| B-9 | normal | plausible | T3 | PATCH /sales ignora si la venta tenía movimiento y empieza a descontar el total de una venta que nunca descontó | PR abierto | #190 |
| E-2 | grave | confirmado | T3 | VOID_DUPLICATE anula ventas y gastos sin devolver su movimiento de stock ni soltar su identidad | PR abierto | #191 |
| E-3 | grave | confirmado | T3 | La corrección de ventas del chat (UPDATE_SALE) cambia cantidad, producto e importe sin ajustar el stock ya descontado | PR abierto | #191 |
| C-2 | grave | confirmado | T3 | balance_reconstruido_a cuenta ventas importadas que nunca descontaron stock: el ajuste de reconciliación de B11 infla el stock | pendiente |  |
| E0-B-2 | grave | e0 | T3 | La merma por chat (REGISTER_STOCK_LOSS) no valida stock: el piso max(0, …) oculta stock negativo y desalinea stock_units del ledger y del balance | mergeado | #189 `19326e2e` |
| C-5 | normal | confirmado | T3 | register_stock_loss acepta una merma mayor que el stock: clampa stock_units a 0 pero registra el movimiento y la auditoría por la cantidad entera | mergeado | #189 `19326e2e` |
| E-1 | grave | confirmado | T4 | El detector de duplicados de DATA_QUALITY_REPAIR anula ventas y gastos legítimos repetidos dentro del mismo archivo | pendiente |  |
| E-5 | grave | confirmado | T4 | El dedup MOST_RECENT pisa el stock del canónico con la cantidad del ancla y borra las ventas y compras posteriores | pendiente |  |
| E-6 | grave | confirmado | T4 | Fusionar productos no re-apunta pos_stock_discrepancies ni product_supplier_links: la salida pendiente del POS se resuelve sobre el duplicado inactivo | pendiente |  |
| E-9 | normal | confirmado | T4 | El dedup crea el balance del canónico con current_qty = delta en vez de stock_units, y descuadra el balance contra el stock | pendiente |  |
| E-4 | normal | plausible | T4 | La misclasificación de catálogos anula ventas de OTRO archivo por ventana de created_at, sin filtrar por source_upload_id | pendiente |  |
| E-8 | normal | confirmado | T4 | B·5g: un renglón de un comprobante con hermanos vivos no puede volver a entrar al re-subir el archivo, contra lo que promete el código | pendiente |  |
| E-11 | normal | plausible | T4 | M·R guarda como monto anulado el de todos los candidatos aunque bajo el lock se anulen menos | pendiente |  |
| A-3 | grave | confirmado | T5 | VIEWER/ANALYST pueden abrir y cerrar turnos ajenos y firmar el arqueo | mergeado | #182 (R-2) |
| A-4 | grave | confirmado | T5 | Anular un ticket de un turno o día ya cerrado no deja egreso de caja: el reintegro descuadra el arqueo de hoy | pendiente |  |
| A-5 | normal | confirmado | T5 | Cierre de caja diario: el sobrante registrado se fecha hoy aunque se cierre otro día, y se acepta una fecha futura | pendiente |  |
| A-6 | normal | confirmado | T5 | cash_received_ars sin decimal_places: el vuelto se calcula con un redondeo distinto del que guarda la base | pendiente |  |
| D-4 | grave | confirmado | T6 | La configuración de margen dice «margen bruto, 35/20 por rubro», pero el motor la aplica al margen NETO | pendiente |  |
| D-5 | grave | confirmado | T6 | El score usa el contado de cualquier arqueo viejo como saldo de caja, aunque es la recaudación neta de un día | pendiente |  |
| D-6 | normal | confirmado | T6 | La ventana del BSL es de 31 días (30 después de las 21 ART), contra 30 del período anterior: el crecimiento queda sesgado | pendiente |  |
| D-8 | normal | confirmado | T6 | El forecast proyecta ventas en los días de la semana en que el negocio no vende y calcula mal la tendencia | pendiente |  |
| D-9 | normal | confirmado | T6 | El sobrestock marca como capital inmovilizado productos que no tienen stock | pendiente |  |
| D-10 | normal | confirmado | T6 | La caja recibe un 60 inventado cuando hay saldo pero no hay gastos fijos, y entra al total con peso 0,30 | pendiente |  |
| D-11 | normal | confirmado | T6 | Los insights nunca se generan: el único que despacha generate_insight es un job que nadie dispara | pendiente |  |
| E0-B-5 | normal | e0 | T6 | insight_templates: 'or 100' convierte score_cash=0 (la peor caja) en 100 y suprime la variante 'Situación crítica' | pendiente |  |
| E0-B-6 | normal | e0 | T6 | Narrativa LLM del insight recibe subscores inventados: caja desconocida persistida como 50 y None→0 con 'or 0' | pendiente |  |
| E0-A-1 | grave | e0 | T7 | Una compra cargada por chat entre las 21 y las 24 ART queda con fecha de mañana y bloquea la venta de hoy de ese producto (B·5e) | pendiente |  |
| E0-A-2 | grave | e0 | T7 | Un gasto o una salida de caja por chat con entidades del CEO queda fechado mañana a las 00:00 entre las 21 y las 24 ART | pendiente |  |
| E0-A-3 | grave | e0 | T7 | El cálculo de 'sin rotación' saca las ventas de hoy (compara un DATETIME contra una fecha) y date.today() lo tapa sólo de 21 a 24 ART | pendiente |  |
| E0-A-4 | normal | e0 | T7 | El asistente de Calendar le dice al LLM que 'hoy' es mañana entre las 21 y las 24 ART | pendiente |  |
| E0-A-5 | normal | e0 | T7 | El cupo diario de 50 mensajes de chat se renueva a las 21:00 ART y no a medianoche | pendiente |  |
| E0-A-6 | normal | e0 | T7 | Las series diarias de los agentes terminan en un día futuro en cero y pierden el día más viejo entre las 21 y las 24 ART | pendiente |  |
| E0-A-7 | normal | e0 | T7 | /sales/summary y el listado por defecto de /sales corren la ventana de 30 días a mañana entre las 21 y las 24 ART | pendiente |  |
| E0-A-8 | normal | e0 | T7 | /expenses/summary, el listado y el conteo por defecto de /expenses corren la ventana un día entre las 21 y las 24 ART | pendiente |  |
| E0-A-9 | normal | e0 | T7 | DeterministicFinance._today() es date.today() pelado: la ventana de 30 días del chat se corre y el 'hasta' informa mañana | pendiente |  |
| E0-A-10 | normal | e0 | T7 | El forecast de caja calculado entre las 21 y las 24 ART arranca un día tarde y queda cacheado 6 h | pendiente |  |
| E0-A-11 | normal | e0 | T7 | ValidationGate calcula el aviso de fecha futura con el día UTC y entre las 21 y las 24 ART tolera dos días | pendiente |  |
| E0-A-12 | normal | e0 | T7 | Los clientes inactivos se marcan unas horas antes entre las 21 y las 24 ART | pendiente |  |
| E0-A-13 | normal | e0 | T7 | Los análisis de productos estrella/problemáticos y de clientes del agente de ingresos corren la ventana de 30 días | pendiente |  |
| E0-A-14 | normal | e0 | T7 | El dashboard de marketing corre un día la ventana de métricas sociales entre las 21 y las 24 ART | pendiente |  |
| E0-A-15 | normal | e0 | T7 | Mail semanal: date.today() no molesta con el horario del beat, pero la 'semana' de los cierres de caja es la que recién empieza | pendiente |  |
| B-8 | normal | confirmado | T7 | date.today() (UTC) en resúmenes, listados por defecto y series diarias | pendiente |  |
| D-7 | normal | confirmado | T7 | date.today() (UTC) en breakdown, cash-breakdown, forecast y DeterministicFinance; «sin rotación» no ve las ventas de hoy | pendiente |  |
| E0-B-1 | grave | e0 | T8 | Recálculo de score encolado con .delay() ANTES del commit en gastos, compras manuales, productos y onboarding | pendiente |  |
| B-7 | normal | confirmado | T8 | Recálculo de score con .delay() antes del commit en gastos, compras y productos | pendiente |  |
| B-6 | normal | confirmado | T8 | POST /purchases/manual no pasa por el gate de suscripción, a diferencia de las otras altas de compra y gasto | pendiente |  |
| B-11 | normal | confirmado | T8 | Altas sin entrada en decision_audit_log: POST /sales, /sales/bulk, /expenses y /products | pendiente |  |
| E0-B-4 | grave | e0 | T8 | Validación 422 loguea en claro contraseñas y PIN (raw_body y exc.errors() con 'input') | mergeado | #175 |
| E0-B-7 | normal | e0 | T8 | Texto controlado por el usuario llega al LLM sin wrap_user_input (celdas y encabezados de archivos subidos, display_name del negocio) | pendiente |  |
| B-10 | normal | refutado | Refutados por el verificador (no se arreglan) | Atrasar la fecha del stock inicial deja ventas vivas sin compra previa sin avisarlo como tal | pendiente |  |
| C-6 | normal | refutado | Refutados por el verificador (no se arreglan) | Anular la única fuente de costo (o una apertura) deja products.unit_cost_ars y su sello de origen apuntando al movimiento anulado, y las ventas nuevas se costean con eso | pendiente |  |
| E-7 | normal | refutado | Refutados por el verificador (no se arreglan) | Revert de B·5g: sin identidad de comprobante, una venta re-importada no se detecta y queda duplicada | pendiente |  |
| E-10 | normal | refutado | Refutados por el verificador (no se arreglan) | Re-aplicar un plan de dedup parcial vuelve a aplicar grupos ya fusionados cuando el duplicado no tenía FKs | pendiente |  |
| I1-1 | critico | confirmado | I-A | El índice de vínculo de ventas y compras incluye productos inactivos: después de una fusión (dedup) o de borrar un producto, las ventas van a «Otros» o quedan en un producto muerto | mergeado | #178 |
| I1-2 | critico | confirmado | I-A | El tier de tokens de _resolve_product saltea las palabras que no existen en el catálogo: compras y ventas de un producto distinto se pegan a otro que comparte una palabra | mergeado | #178 |
| I6-1 | critico | confirmado | I-A | Import de clientes/proveedores: un email o teléfono compartido o de relleno ('-', '0') pisa la ficha de OTRA persona aunque el documento sea distinto | mergeado | #179 |
| I6-3 | grave | confirmado | I-A | La clave por ID externo no incluye la entidad: en una tabla mixta, la venta reclama la clave del gasto y TODOS los gastos se omiten en silencio como 'ya aplicados' | pendiente |  |
| I6-4 | grave | confirmado | I-A | La letra fiscal se descarta de la clave: Factura A y Factura B con el mismo punto de venta y número colisionan (en AFIP numeran por separado) | pendiente |  |
| I6-7 | grave | confirmado | I-A | Resolver una venta o gasto desde «Otros» no reclama la identidad del comprobante: un reimport posterior del mismo documento lo vuelve a aplicar (duplicado) | pendiente |  |
| I5-4 | grave | confirmado | I-A | sku y barcode no son SINGLE_VALUE: dos columnas al mismo código de identidad se resuelven por orden de columna, sin aviso | pendiente |  |
| I6-2 | grave | confirmado | I-A | Identidad de operación: sentencias sin tandas pasan el tope de 32.767 parámetros de asyncpg; falla el confirm de archivos grandes y el borrado/relectura de cualquier archivo con más de ~16k operaciones | pendiente |  |
| I6-10 | normal | plausible | I-A | Celdas numéricas float de xlsx generados por script ('123.0', '1024.0') producen claves de identidad falsas o distintas | pendiente |  |
| I3-1 | grave | confirmado | I-B | La relectura (preview, apply y undo) no respeta el borrado del archivo: un archivo borrado vuelve a tener ventas, gastos y stock vivos, y ya no se pueden revertir | pendiente |  |
| I4-1 | grave | confirmado | I-B | La relectura se aplica sobre un archivo YA BORRADO y resucita todos sus datos, sin forma de volver a borrarlo | pendiente |  |
| I4-2 | grave | confirmado | I-B | «Deshacer relectura» funciona sobre un archivo borrado: des-anula sus ventas, gastos y stock originales | pendiente |  |
| I4-3 | grave | confirmado | I-B | Deshacer relectura borra (hard delete) ventas y gastos que el usuario editó DESPUÉS de releer | pendiente |  |
| I4-4 | grave | confirmado | I-B | El undo de relectura no revierte «Otros»: quedan pendientes que duplican ventas restauradas | pendiente |  |
| I4-5 | grave | confirmado | I-B | Borrar un archivo, deshacer o releer deja stock_units NEGATIVO (void_movement sin piso ni chequeo) | pendiente |  |
| I4-6 | grave | confirmado | I-B | La relectura nunca vuelve a leer las filas que el import anterior mandó a «Otros» (sin fecha, sin monto, total, conflicto, venta sin stock o sin compra) | pendiente |  |
| I3-6 | normal | confirmado | I-B | «La relectura respeta el lease del import» se verifica sólo en el endpoint: el worker de la relectura aplica sin volver a mirar el lease | pendiente |  |
| I4-7 | normal | confirmado | I-B | Con autoflush=False, el borrado ve como «vivas» las ventas y gastos del propio archivo y conserva sus maestros y productos con un motivo falso | pendiente |  |
| I4-8 | normal | confirmado | I-B | El borrado ignora lo que hizo una relectura sobre clientes y proveedores (REREAD_MASTER_*), y la relectura no registra los campos cruzados de B·6 | pendiente |  |
| I4-9 | normal | confirmado | I-B | El borrado cuenta las filas DISMISSED de «Otros» como «clasificadas sin procedencia»: falso `fully_reverted: false` y filas colgadas de un archivo borrado | pendiente |  |
| I4-10 | normal | confirmado | I-B | Un cliente o proveedor creado por el archivo se desactiva aunque OTRO archivo vivo también lo trajo | pendiente |  |
| I4-11 | normal | plausible | I-B | El sweep y el guard vencen runs QUEUED/APPLYING sin CAS: pueden pisar un reclamo del worker y habilitar dos applies a la vez | pendiente |  |
| I4-12 | normal | plausible | I-B | Ni el confirm ni el worker de relectura se excluyen entre sí: el lease sólo se mira al encolar | pendiente |  |
| I4-13 | normal | confirmado | I-B | El aviso de «carga repetida masiva» cruza ventas con gastos y cuenta de más cada clave repetida | pendiente |  |
| I4-14 | normal | confirmado | I-B | El diagnóstico de cola mide la antigüedad con `created_at`: una sesión reusada aparece «trabada» al instante | pendiente |  |
| I1-3 | grave | confirmado | I-C | Una compra de mercadería con cantidad decimal de un producto por unidad que ya existe deja un gasto COGS sin stock ni movimiento, sin avisar | pendiente |  |
| I1-4 | grave | plausible | I-C | Las filas que F8 manda a «Otros» se borran del bucket y corren los índices: su __row_ref__ coincide con el source_row_ref de la fila siguiente, y la relectura anula la venta o gasto clasificado a mano sin medirlo como pérdida | pendiente |  |
| I2-1 | grave | confirmado | I-C | El costo del envío se cobra de nuevo en otro archivo aunque todas sus compras quedaron como «ya aplicadas» (E6b) | pendiente |  |
| I2-2 | grave | confirmado | I-C | El gate de replay cuenta como consumo ventas que después no se insertan (ya aplicadas, salteadas por huella, en conflicto) | pendiente |  |
| I2-3 | grave | confirmado | I-C | Tabla suelta con Ventas+Productos tildados: la cantidad vendida de cada fila pasa a ser el stock absoluto del producto | pendiente |  |
| I2-6 | grave | confirmado | I-C | Tabla suelta: el tratamiento de stock por hoja ({context_id: ...}) se ignora y la compra se registra como apertura | pendiente |  |
| I1-5 | normal | confirmado | I-C | Gate de historical_replay con productos por peso: compara la cantidad de la venta en kg contra el saldo en gramos y nunca frena | pendiente |  |
| I2-7 | normal | confirmado | I-C | Gate de replay: compara la cantidad en unidad de venta contra un saldo en unidades base (productos por peso/volumen) | pendiente |  |
| I1-6 | normal | confirmado | I-C | Una venta importada con cantidad declarada 0 entra como 1 unidad | pendiente |  |
| I1-7 | normal | confirmado | I-C | B·5b-2 exige NOMBRE: una compra de mercadería identificada sólo por SKU o código de barras y con cantidad va a «Otros», cuando la doc dice 'nombre o código' | pendiente |  |
| I2-8 | normal | confirmado | I-C | Un catálogo que declara stock 0 no pisa el saldo existente: el producto agotado sigue con stock | pendiente |  |
| I2-4 | grave | confirmado | I-D | «Importar sugeridos» persiste fechas futuras y relee el original celda por celda, sin el convenio de su columna | pendiente |  |
| I2-5 | grave | confirmado | I-D | «Importar sugeridos» deja entrar lo que el import mandó a «Otros» a propósito: COGS sin artículo, venta sin stock, y ventas sin movimiento de stock ni procedencia | pendiente |  |
| I2-9 | normal | confirmado | I-D | «Importar sugeridos» no bloquea las filas: en concurrencia con reclassify/link-product/dismiss duplica la venta o pisa el estado | pendiente |  |
| I6-8 | normal | confirmado | I-D | La importación en lote de «Otros» lee los PENDING sin FOR UPDATE: compite con reclassify/dismiss y duplica la venta o importa lo descartado | pendiente |  |
| I5-1 | grave | confirmado | I-E | Los números nativos de Excel se pasan a texto en el parseo y el convenio de la columna los vuelve a interpretar: un valor con 3 decimales entra multiplicado por 1000 sin aviso, o la fila va a «Otros» | pendiente |  |
| I5-2 | grave | confirmado | I-E | Dos encabezados repetidos (o dos vacíos en un CSV) se pisan en el dict de la fila: los datos de la primera columna se pierden y el mapeo de las dos colapsa en una | pendiente |  |
| I5-3 | grave | confirmado | I-E | El reconocedor de encabezados ignora calificadores que cambian el significado y propone «unico» un destino que mueve plata o stock | pendiente |  |
| I6-5 | grave | confirmado | I-E | Convenio de fecha por columna roto cuando las fechas traen hora: una columna mm/dd con hora se interpreta celda por celda (mezcla dmy y mdy) | pendiente |  |
| I6-6 | grave | confirmado | I-E | Una fecha ISO con zona horaria ('…Z', '…-03:00') entra como datetime con zona y revienta el INSERT del import en Postgres | pendiente |  |
| I5-5 | normal | confirmado | I-E | Con ENABLE_LLM_FILE_TYPE_DETECTION prendido, el detector cambia inferred_type y la entidad del contexto pero deja las filas en otros_detectados: archivos detectados como stock, clientes o proveedores no importan nada, y con cualquier confianza | pendiente |  |
| I5-6 | normal | confirmado | I-E | La 4ª capa LLM de mapeo puede proponer campos de identidad que E6b prohíbe autosugerir; con cualquier confianza propia > 0 pisa columnas sin lectura, y lo aceptado sin tocar se aprende como historial del tenant | pendiente |  |
| I5-7 | normal | confirmado | I-E | Bloque 5 guarda como «decisión explícita» el mapeo completo (sugerencias y el ignore automático de B·5f) y confunde hojas con los mismos encabezados | pendiente |  |
| I5-9 | normal | confirmado | I-E | Dos confirms simultáneos del mismo tenant que aprenden el mismo alias nuevo chocan con el índice único y se revierte el import entero | pendiente |  |
| I6-9 | normal | confirmado | I-E | El evaluador de fórmulas no corta la recursión: una fórmula con muchos paréntesis anidados tira RecursionError y hace fallar el parseo del archivo entero | pendiente |  |
| I5-8 | normal | confirmado | I-E | Inyección de fórmulas: el texto importado se guarda tal cual y la exportación CSV no lo neutraliza | pendiente |  |
| I6-11 | normal | confirmado | I-E | La extracción tabular de remitos toma la fila «Total» como un artículo e interpreta los números celda por celda | pendiente |  |
| I6-12 | normal | confirmado | I-E | Los lectores de planillas de remitos e import de clientes/proveedores no aplican los topes de ingestion_limits (tamaño expandido, filas, tiempo) | pendiente |  |
| I3-3 | grave | confirmado | I-F | Un VIEWER puede subir, confirmar e importar archivos (también por la ruta asíncrona), reprocesar y cancelar: los endpoints de ingesta no tienen require_role | mergeado | #176 |
| I3-2 | grave | confirmado | I-F | jobs.execute_import hereda los límites globales de Celery (240 s soft / 300 s hard): un import grande por la ruta asíncrona no termina nunca y deja el archivo y el intento trabados | pendiente |  |
| I3-4 | grave | confirmado | I-F | La importación asíncrona nunca dispara el recálculo del score: el ejecutor le pasa a confirm_file un BackgroundTasks() que nadie ejecuta | pendiente |  |
| I3-5 | normal | confirmado | I-F | La compensación del ejecutor libera el lease de OTRA importación que tiene el archivo | pendiente |  |
| I3-7 | normal | confirmado | I-F | Un archivo cuyo import murió queda IMPORTING para siempre y DELETE lo rechaza aunque el lease esté vencido | pendiente |  |
| I3-8 | normal | confirmado | I-F | POST /files/upload con un purpose distinto de 'chat' crea un archivo de ingesta sin content_hash, sin traza y sin gate de suscripción, que después se puede importar salteando el anti-duplicado | pendiente |  |

## Revisiones de PR (R-1…R-11)

| Id | Sev. | Hallazgo | Estado | PR / nota |
|---|---|---|---|---|
| R-1 | grave | chat auto-ejecuta escrituras sin mirar el rol | mergeado | #177 |
| R-2 | normal | VIEWER abre o cierra turnos de caja (= A-3) | mergeado | #182 `587ff05e` |
| R-3 | normal | VIEWER crea una automatización desde una acción aprobada | mergeado | #183 `3b51b78d` |
| R-4 | normal | VIEWER da de alta un campo propio | mergeado | #184 `4a21e4cd` (incluye el toggle que creaba campos) |
| R-5 | menor | cualquiera cancela la acción pendiente de otro | mergeado | #185 `7527b131` |
| R-6 | menor | subida a S3 sin gate con purpose distinto de chat (relacionado con I3-8) | mergeado | #186 `628d7b6c` (I3-8 sigue abierto) |
| R-7 | menor | 402 antes que 403 cuando el rol va como parámetro | mergeado | #187 `2f9d0bfc` |
| R-8 | menor | el 403 de require_role está en inglés | mergeado | #188 `6e2ede3b` |
| R-9 | menor | serie diaria «caja» del agente de salud es ventas − gastos | pendiente |  |
| R-10 | normal | forecast cuenta la tarjeta de crédito el día de la venta | pendiente | PR aparte, estimación explícita y configurable |
| R-11 | normal | sobrante del arqueo guardado como inflow y rotulado «Cobro cta. cte.» | pendiente | PR aparte |

## Barrido de críticos, Etapas 3–6 (2026-10-26, sobre `6e2ede3b`)

Barrido sólo de críticos (seguridad, cruce de tenants, pérdida de plata); la revisión completa de 3–6 queda para después del 30/10.

| Id | Sev. | Estado hallazgo | Hallazgo | Estado | PR / nota |
|---|---|---|---|---|---|
| X3-1 | crítico | confirmado | Un usuario dado de baja se reactiva solo: resend-verification + verify-email ponen is_active=True y devuelven JWT (mismo rol, PIN y permisos; saltea el cupo de plazas) | PR abierto | #195 |
| X6-1 | crítico | confirmado | La cola offline de carga manual (localStorage sin tenant/usuario) se sube con el JWT del siguiente usuario que entra en esa PC: los datos de A quedan en el negocio B | PR abierto | #193 |
| X6-2 | crítico | confirmado | Conversación del chat cruzada: el store persiste conversationId y mensajes sin dueño, y conversation_service.get_context/persist no validan tenant (lee y pisa turnos de otro negocio) | PR abierto | #196 |
| X4-1 | grave | plausible | La huella anti-duplicado del chat (tenant+acción+monto+fecha, sin vencimiento) descarta ventas/gastos/compras legítimas; con fecha nula colisiona para siempre | PR abierto | #194 |
| X6-3 | normal | confirmado | Sentry recibe X-POS-Terminal (secreto de la caja): sentryScrub no lo cubre | pendiente |  |
| X3-2 | normal | confirmado | Cambiar/resetear contraseña no revoca refresh tokens (7 días) | pendiente |  |
| X5-1 | normal | plausible | purge_deleted_file_data.py y void_misclassified_imports.py anulan sin chequear stock negativo / sin revertir movimientos | pendiente |  |

Etapa 5 (persistencia, migraciones, scripts): sin críticos (una sola head, lock de migración correcto, migraciones recientes aditivas, scripts con dry-run).
