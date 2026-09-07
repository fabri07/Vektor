**Plan integral para corregir y completar la ingesta de Vektor.** La propuesta conserva los parsers, los servicios de identidad, las reglas de inventario y la trazabilidad existentes, y reemplaza gradualmente los caminos de interpretación y ejecución que hoy divergen.

Fecha: 2026-09-06. Base revisada: `a62270bf`. Estado: planificación; no implementado. La revisión anterior ejecutó 851 pruebas de backend sobre SQLite y 237 de frontend, todas satisfactorias, además de reproducciones aisladas de defectos. Esa revisión no ejecutó PostgreSQL ni inspeccionó producción — el CI sí tiene un job PostgreSQL propio (`ci-backend.yml`, `postgres:16` + `TEST_PG_DSN`, marker `postgres`), así que lo que falta es cobertura de los escenarios nuevos, no la infraestructura para correrlos. Esos resultados no son evidencia de que este programa ya esté validado.

**Objetivo verificable.** Cada archivo debe producir una interpretación revisable y un resultado trazable: respetar decisiones explícitas, conservar datos originales, justificar exclusiones, cuadrar importes y stock, tolerar reintentos y permitir una relectura o reversión segura. La pantalla, la confirmación y la relectura deben compartir las reglas y la interpretación de los datos.

**Alcance.** Carga, almacenamiento, parsing, clasificación, mapeo, normalización, validación, vista previa, confirmación, ejecución, identidad, deduplicación, inventario, costos, clientes/proveedores, bandeja Otros, relectura, borrado/reversa, observabilidad y reparación histórica. CSV/XLSX será la primera ruta migrada. Los formatos de texto, documentos e imágenes ya admitidos y los callers de chat, remitos y reparación se inventariarán y adaptarán al mismo contrato cuando generen operaciones de negocio. Una extracción documental para contexto debe conservar su finalidad y no convertirse automáticamente en una venta o gasto.

**Límites del programa.** No incorpora nuevos conectores ni promete interpretar cualquier documento sin intervención. Tampoco modifica reglas contables por conveniencia técnica. Una capacidad no soportada debe quedar explicada y conservar el dato para revisión. Las fechas objetivo se fijarán después de medir el alcance en F0; la secuencia siguiente expresa dependencias, no un calendario comprometido.

**Evidencia y pendientes que debe cubrir.** Se distingue entre lo reproducido, lo observado en código y los pendientes de documentos que todavía necesitan comprobación actual.

| ID | Hallazgo | Evidencia / estado inicial | Entrega responsable |
|---|---|---|---|
| H01 | El importador vuelve a usar columnas marcadas `ignore` | Reproducido en SQLite llamando al servicio: `cantidad=7`, mapeo `ignore`, venta guardada con 7 unidades. También se reprodujo con el total. Falta cubrir el recorrido HTTP completo. | F1, F3 |
| H02 | La relectura pierde un cambio de entidad si no se mandan mapeos de columnas; elimina mapeos `ignore`. **Y aguas arriba el endpoint no persiste el borrador sin `column_mappings`** | Reproducido llamando a `_draft_effective_mappings` (`reread_service.py:2377-2379` sale antes de leer `context_entities`; `:2388` filtra los `ignore`). El eslabón del endpoint es `ingestion.py:3604`: un body con sólo `context_entity` devuelve 200 y no guarda nada | F1, F6 |
| H03 | `12.500` se interpreta como 12,5 sin resolver el convenio numérico. Ningún parser existente sirve de referencia: los tres fallan distinto | Reproducido: `_parse_amount("12.500")` → 12,5 (sin rama para solo-punto) y `"1.234.567"` → `None`; `analytics.parse_money("12,500.00")` → 12,5 (asume AR sin comparar cuál separador viene último); `validation_gate._extract_amounts` borra TODOS los puntos → `12.5` → 125, incluso sobre celdas numéricas nativas. Además `_parse_qty("1.500")` → 1 (10 call sites) y `_parse_amount` deriva a "Otros" todo `<= 0` | F2 |
| H04 | Compras con envío tienen distintas capacidades entre tabla única y multihoja; la UI y el backend usan predicados diferentes | Código actual; rechazo protector vigente en el confirm plano | F3, F4, F7 |
| H05 | Se publica el trabajo de parsing antes del commit del archivo | Carrera posible observada en código; falta prueba con dos sesiones PostgreSQL | F1, F5 |
| H06 | `max_retries=3` no está acompañado por `retry()` ni `autoretry_for` | Código y atributos efectivos del worker revisados | F1, F5 |
| H07 | Confirmación HTTP prolongada; lease sin renovación periódica en ese camino | Código; defaults revisados de 15 minutos de lease y 16 de cliente. Configuración y proxy productivos no medidos. **El fencing por token SÍ existe y se verifica** (`finalize_import_lease:195-213`, `rowcount != 1` → `ImportLeaseLostError` → 409, sin commit propio): el riesgo es desperdicio de trabajo y contención, no corrupción | F0, F5 |
| H08 | Parsing materializa el archivo; deduplicación precarga huellas de todo el tenant | Código actual; crecimiento a medir | F0, F3, F5 |
| H09 | Alta complejidad y reglas repartidas entre importador, endpoint, relectura y frontend | Importador 7.745 líneas; endpoints 4.048; panel 2.602; relectura 4.000. La responsabilidad compartida, no el número de líneas, es el criterio de extracción | F3–F7 |
| H10 | Hay capacidades detrás de flags vacías por defecto; la procedencia de decisiones sugeridas/confirmadas necesita una regla común. El inventario va **por servicio**, no por entorno: API, worker y procesos periódicos tienen que interpretar los datos con reglas y configuración compatibles | Cuatro flags de rollout, todas `[]` por default (`config/settings.py:292-324`); una de ellas gatea la persistencia de decisiones de esquema. Producción no inspeccionada | F0, F3, F7, F8 |
| H11 | El camino legacy de altas no consulta la cota del lote | Verificado: el único `_batch_productos.lleno` está en el loop por contexto (`:7291`). **No hay pérdida de datos** — el `_flush_batch_productos()` de `:7388-7392` es incondicional y drena por chunks. El defecto es memoria no acotada durante el loop y un savepoint gigante al final, que agranda el blast radius de un `SavepointConflictError` | F0, F5 |
| H12 | Una repetición del archivo Asteria agregaba 26 movimientos `adjustment` | Pendiente documentado; determinar si son redundantes, cambian saldos o reflejan un efecto legítimo | F0, F6, F8 |
| H13 | 538 casos `historial_sin_fecha` y falta de prueba dedicada del rollback de `apply_reread` | Pendientes documentados; no asumir que las 538 filas son errores del parser | F0, F2, F6 |
| H14 | Códigos externos, nombres de personas, ruteos entre entidades y simetría de referencias tienen alcance pendiente en planes previos | Contrastar contra código y tests; no reimplementar partes ya resueltas | F0, F4 |
| H15 | El worker de parseo escribe estado sin comprobarlo: `_load_and_lock` hacía un `SELECT` plano —sin `FOR UPDATE`, pese al nombre— y asignaba `PROCESSING` sin guard, sin `rowcount` y sin filtrar `deleted_at` | Verificado en `jobs/ingestion_worker.py:60-80`. Con `task_acks_late=True` una re-entrega devuelve a `PROCESSING` un archivo `DONE` y lo deja en `NEEDS_CONFIRMATION`, estado que el CAS de `acquire_import_lease` acepta. **Si eso deriva o no en efectos económicos duplicados es un escenario a reproducir**, no un daño atribuido: la huella de dedup es `sha256(tenant:IMPORT_ROW:file_id:context_id:row_index)` y establecer que los cubre a todos exige recorrer cada efecto, no sólo las filas que llevan huella | F1 (E1) |
| H16 | Recuperación de trabajos huérfanos y observabilidad periódica: verificar qué corre realmente | `jobs.sweep_stale_reread_runs` está programado cada 10 min y ubicado a propósito en la cola `scores` para no trabarse con lo que audita; existe `backend/beat/railway.toml`. **El código no demuestra qué está desplegado**: es una verificación operativa pendiente, con método explícito | F0, F8 (E5) |

Las referencias de implementación son `backend/app/application/services/ingestion_import_service.py`, `reread_service.py`, `file_parsing.py`, `ingestion_lease_service.py`, `backend/app/api/v1/ingestion.py`, `backend/app/jobs/ingestion_worker.py` y `frontend/src/features/ingestion/ColumnMapperPanel.tsx`. Los pendientes anteriores provienen de [ingestion-mapping-overhaul.md](ingestion-mapping-overhaul.md), [asteria-bloques-ingestion.md](asteria-bloques-ingestion.md) y [confirm-import-n1-y-mapeo.md](confirm-import-n1-y-mapeo.md). Sus afirmaciones sobre producción son históricas, no verificaciones actuales.

**Contratos obligatorios para todas las fases.** Se documentan antes de cambiar el comportamiento y se prueban en los límites entre componentes:

- La detección automática nunca modifica una decisión explícita. `ignore`, `unmapped`, sugerencia y confirmación son estados distintos. Aceptar explícitamente un conjunto de sugerencias debe registrarse como una acción del usuario.
- Los valores originales, su ubicación y su interpretación se conservan. No se inventan fechas, importes ni cantidades para hacer pasar una validación.
- Cada fila fuente tiene un resultado explícito: aplicada, ya aplicada, pendiente de revisión o excluida con motivo. Una fila puede generar varias entidades/movimientos; el conteo de filas fuente y el de efectos se reconcilian por separado. Filas vacías, encabezados y resúmenes también se identifican.
- Con el mismo plan y el mismo estado de negocio, preview y ejecución producen el mismo resultado. Si cambian stock, identidades o ediciones manuales entre ambos, se revalidan bajo transacción: un conflicto que cambia materialmente el resultado exige nueva revisión, no una sustitución silenciosa.
- Reintentar una ejecución no duplica efectos. Releer sin cambios conserva el estado económico y no agrega movimientos redundantes, aunque genere auditoría de un nuevo intento.
- La publicación de operaciones de un archivo es atómica. Procesar lotes o reanudar preparación no autoriza a exponer medio archivo en ventas, caja o stock.
- Todas las claves, consultas, planes, archivos y operaciones están aislados por tenant. Ninguna reparación sobrescribe ediciones posteriores sin identificar el conflicto.

**F0 — Inventario, corpus y línea de base.** Antes de cerrar estimaciones, se prepara una referencia que permita evaluar cada cambio.

Trabajo:

- Inventariar todos los callers de `insert_confirmed_data`, los contratos HTTP, los estados persistidos, los jobs, los límites y las flags. Mapear qué capacidades están implementadas, probadas, habilitadas por defecto y pendientes de comprobar en el entorno de despliegue.
- Conciliar los planes anteriores con el código. Marcar cada pendiente como vigente, resuelto, sustituido o fuera del alcance admitido, con evidencia.
- Ampliar y organizar el corpus existente hasta hacerlo versionado, con archivos sintéticos y ejemplos reales autorizados/anonimizados. **No se parte de cero**: nueve archivos de test ya generan XLSX con `openpyxl` y los pasan al parser de producción (`test_ingestion_replay_end_to_end.py:71` arma un libro de tres hojas y corre el e2e completo). Lo que falta es cobertura y organización, no el andamiaje. Incluir CSV con distintos separadores y encodings; Excel de una o varias hojas; libro diario; catálogos; compras con flete; ventas; clientes/proveedores; hojas mixtas, resúmenes y columnas desconocidas; formatos documentales admitidos.
- Fijar resultados esperados revisados con criterio de negocio: filas, montos, entidades vinculadas, stock inicial/final, costos, advertencias y efectos de reversión. El sistema viejo no será el oráculo cuando se conozca un defecto.
- Reproducir H11–H14. Para Asteria, rastrear cada ajuste adicional y cada caso sin fecha hasta su fila original.
- Medir por etapa: espera de cola, parsing, preview, validaciones, resolución, inserción y publicación; memoria máxima, número de consultas por forma SQL y tiempo con historial del tenant creciente. Usar archivos pequeños, representativos y cercanos al límite soportado.
- Registrar límites operativos y objetivos de latencia/memoria sobre infraestructura identificada. El benchmark documentado de 251 consultas / 4,2 s es una referencia histórica, no un objetivo universal ni una medición productiva actual.

Salida: matriz de capacidades, corpus con resultados esperados, lista final de defectos y presupuestos de rendimiento. La fase termina cuando cada H01–H14 tiene prueba reproducible o una incertidumbre explícita con método de verificación.

**F1 — Correcciones urgentes en los caminos actuales.** No se espera a la nueva arquitectura para corregir decisiones ignoradas o carreras de ejecución.

Trabajo:

- Centralizar la resolución de columnas y hacer que **todos** los lectores la consuman. La forma elegida es sacar la columna de la fila en el punto de entrada (`_sin_columnas_ignoradas`, aplicado en los dos chokepoints: `_filas_y_mapeo` del multihoja y la entrada de filas del camino plano) en vez de agregarles un parámetro: si la clave no está en la fila, ningún lector puede encontrarla, y un lector nuevo queda cubierto por construcción. Un parámetro más habría que acordarse de pasárselo. `ignore` no puede seguir colapsado con "sin mapear" (`_resolve_target_cols:3143-3146` lo saltea con un `continue`, pese a que `ParsedTarget` distingue los dos a propósito): así queda fuera de `target_to_col`, de `_reservadas` y del `skip` del monto, y las heurísticas lo vuelven a leer. Excluirlo de `headers` NO alcanza — varios lectores van directo al dict de la fila: `_row_val` (`:3077`), `_row_col` (`:3044`), `_val` del multihoja (`:5525`), `_resolve_sale_price_col(list(row.keys()), ...)` (`:6376`) e `import_unclassified_records` (`:7520-7568`, que reimporta "Otros" sin consultar mapeo alguno). El conjunto de ignoradas pasa a parámetro **obligatorio** de cada lector: un default vacío deja al próximo call site repitiendo el bug en silencio. Aplicar la exclusión también a campos opcionales, personalizados y referencias, y conservar el original para auditoría sin usarlo en efectos de negocio.
- Resolver `context_entities` de forma independiente a `column_mappings`; mantener `ignore` en el borrador y durante preview/apply. Incluye el eslabón de arriba: `ingestion.py:3604` envuelve todo el bloque que persiste el borrador en un `if body.column_mappings:`, así que un body con sólo `context_entity`, `context_confirmed` o `stock_treatment` responde 200 sin guardar nada.
- Auditar qué decisiones conserva realmente la relectura: mapeos de todas las entidades, inclusión de hojas, stock, envío y costo. Corregir omisiones comprobadas; cuando un archivo antiguo no tenga información suficiente, presentar una revisión explícita.
- Publicar el trabajo de parsing después de confirmar la existencia del archivo. El estado recuperable ya existe y es `PENDING`: si la publicación falla después del commit, `reprocess_file` lo reencola. Lo que se cierra en E3 es la CARRERA (el worker abre su propia sesión y podía no ver la fila); la ventana entre el commit y la publicación la cierra la cola persistida de F5 (E6c).
- Incorporar adquisición atómica de trabajo y protección de transiciones: un mensaje duplicado no puede devolver un archivo terminado a PROCESSING ni reemplazar su resultado.
- Reintentar solo errores transitorios de red/servicios, con espera creciente y límite. Los errores permanentes de formato o validación deben tener una salida clara. No agregar reintentos antes de proteger las ejecuciones duplicadas. **El default es no reintentar**: la lista de transitorios se declara explícita (corte de conexión, 5xx/429 de S3, conexión de base invalidada) y todo lo demás cierra en `FAILED` — un archivo roto va a fallar igual las tres veces, y reintentarlo sólo retrasa el diagnóstico. Y encender los reintentos obliga a **liberar el trabajo antes de reintentar**: el claim de E1 sólo toma archivos en `PENDING`, así que un segundo intento encontraría el archivo en el `PROCESSING` que dejó el primero y saldría sin hacer nada. La liberación lleva el mismo fencing por token que el resto del ciclo.

Aceptación: pruebas de regresión que fallan antes del arreglo y pasan después, incluyendo recorrido HTTP con `cantidad=7 → ignore`, cambio exclusivo de entidad en relectura y una tarea que intenta leer antes del commit con PostgreSQL real. Doble entrega y retry no generan efectos repetidos. Se mantienen los rechazos protectores de funciones todavía no implementadas.

**F2 — Normalización única y pérdida de datos explícita.** Se centralizan importes, cantidades, fechas, identificadores y valores vacíos.

Trabajo:

- Distinguir celdas numéricas nativas de Excel de números almacenados como texto. Las primeras no se reinterpretan por su formato visual.
- Resolver el convenio de decimales/miles por columna, con sugerencia explicada y decisión persistida. Un valor como `12.500` no se convierte por una regla global de eliminar puntos. Mezclas incompatibles quedan identificadas por fila.
- Usar `Decimal` para cantidades monetarias y una política única de precisión/redondeo. Rechazar valores no finitos. Conciliar total declarado, precio por cantidad, descuentos, impuestos y flete sin aplicar dos veces un componente.
- Preservar y validar cantidades fraccionarias, cero, negativas o ilegibles. Si el dominio de una entidad solo permite enteros, explicarlo y rechazar/revisar la fila; no truncar ni convertirla silenciosamente a 1. Definir el tratamiento de devoluciones/notas de crédito dentro de las capacidades admitidas.
- Reutilizar el parser de fechas compartido, incorporando pruebas de formatos ambiguos, fechas Excel y fechas sin año. Inspeccionar los casos históricos sin fecha antes de ampliar heurísticas. El timezone del negocio se aplica de forma consistente.
- Tratar códigos como identificadores: conservar ceros iniciales y evitar convertirlos a números. Mantener las reglas de campos vacíos, booleanos y encabezados duplicados en un único contrato.
- Exponer original, interpretado y motivo del problema en preview y Otros. Eliminar normalizaciones paralelas solo después de comparar los callers. **La política se define contra el corpus, no se copia de un parser existente**: los tres candidatos fallan distinto (ver H03), así que adoptar cualquiera importaría su error. Hasta que la política exista, la misma celda vale distinto en ingesta, en análisis y en el gate — y el gate, al medir sobre un valor propio, tampoco puede detectar la divergencia.

Aceptación: corpus numérico/temporal aprobado; el valor `12.500` ambiguo necesita resolución, mientras `12.500,00`, `12,500.00` y celdas numéricas inequívocas mantienen su valor. Ninguna cantidad o fecha ilegible se sustituye sin dejar una decisión visible.

**F3 — Un plan de importación común, persistido y versionado.** Se introduce el núcleo que reemplazará los dispatches divergentes.

Flujo propuesto: archivo original → estructura fuente → normalización y decisiones → plan de importación → preview → ejecución del plan → resultado y ledger.

El contrato del plan incluirá:

| Componente | Contenido mínimo |
|---|---|
| Fuente | tenant, archivo/hash, referencia al original, versión del parser, hoja/contexto y ubicación estable de cada fila |
| Decisiones | columnas y entidades destino, inclusión, estados de decisión, procedencia, formato numérico/temporal, inventario, envío y costos |
| Filas normalizadas | valores tipados, referencias a originales, validaciones y resultado previsto; particionadas si el volumen lo requiere |
| Efectos | operaciones y dependencias entre maestros, transacciones, costos, movimientos y vínculos, con claves de idempotencia |
| Revisión | versión/hash del plan, usuario/acción de confirmación, estado de negocio relevante y conflictos que lo invalidan |
| Reproducibilidad | versiones de reglas y configuración efectiva de capacidades; una flag cambiada después no modifica un plan ya confirmado |

Trabajo:

- Definir modelos tipados y migraciones aditivas para planes/intentos/resultados. Reutilizar modelos existentes cuando sus contratos coincidan; no reutilizar `DataRepairRun` como un contenedor genérico sin verificar sus consumidores. **`ingestion_schema_decisions` no es el plan de importación**: recuerda preferencias reutilizables por huella de esquema y se actualiza por upsert, mientras que el plan conserva exactamente qué se confirmó para un archivo y una ejecución, inmutable. Se reutilizan sus servicios y contratos (`compute_schema_fingerprint`, `format_version`, la forma del payload); no se mezclan los registros.
- Separar adaptadores de entrada, normalización, planificación, validación y ejecución. El ejecutor no adivina columnas: consume el plan confirmado.
- CSV y Excel de una hoja se representan como un conjunto de contextos de tamaño uno. Multihoja utiliza exactamente los mismos contratos.
- Hacer que el backend publique capacidades, campos requeridos y efectos; el frontend deja de inferir si un archivo sigue el camino plano o multihoja.
- Adaptar summaries legacy a ese contrato mediante una capa limitada. No mantener dos motores independientes de reglas.
- Preparar y persistir datos por particiones; no introducir otro JSONB gigante para resolver el problema del summary gigante actual. Durante la transición, aplicar cotas claras a parsers que todavía materializan el archivo.
- Enlazar preview y confirmación por versión del plan. Revalidar estado mutable bajo las mismas protecciones transaccionales del ejecutor.

Aceptación: una tabla CSV y la misma tabla en Excel de una o varias hojas producen efectos equivalentes. Cambiar el orden de hojas no cambia identidades ni saldos. Dos previews no escriben datos de negocio. Un plan desactualizado provoca un conflicto explicable. Los formatos legados tienen un adaptador probado o una revisión requerida explícita.

**F4 — Completar identidad, costos y ruteo dentro del núcleo común.** Se conserva la lógica de dominio existente y se completan los huecos funcionales verificados.

Trabajo:

- Completar códigos externos para maestros/productos según F-I, con migración aditiva, ámbito de tenant y reglas de normalización. Diferenciar una fila repetida idéntica de una contradicción de identidad: jamás resolver conflictos por orden de aparición. Prevalidar duplicados antes de imponer índices únicos.
- Resolver referencias con claves fuertes y contradicciones explícitas. Las coincidencias solo por nombre pueden proponer candidatos; no deben fusionar automáticamente identidades ambiguas. Conservar las protecciones de los sentinelas Local y No identificado.
- Separar vinculación y creación de maestros. Revisar el default legacy de proveedores frente a la política de clientes y migrarlo con impacto visible; no cambiarlo globalmente sin comprobar registros existentes.
- Completar ruteos entre entidades usando una allowlist y operaciones tipadas. No habilitar un producto cartesiano de campos ni permitir modificar stock a través de atributos de una venta.
- Completar nombres de personas cuando corresponda: preservar el nombre original, no partir razones sociales y mostrar la separación propuesta antes de aceptarla.
- Unificar costo de compra, agrupamiento por comprobante, envío de línea/compartido, descuentos, impuestos y tratamiento de apertura/compra. Habilitar F-H6.f para tabla única después de probar paridad. Mantener origen y componentes del costo en el plan/ledger, con precisión suficiente para explicar y revertir el resultado.
- Preservar la separación entre identidad del producto y disponibilidad temporal de stock. Extender los tests existentes de replay, faltantes y compras posteriores a ventas al ejecutor común.

Aceptación: igualdad de resultados entre formatos; compras con envío cuadran en gasto/caja, costo y stock según la política confirmada; referencias del mismo código no crean maestros distintos; contradicciones no se fusionan; nombres empresariales se conservan; cada efecto cruzado tiene origen y reversa comprobables.

**F5 — Ejecución duradera, acotada y observable.** La confirmación pasa a registrar una intención de trabajo y devuelve un identificador consultable, sin depender de una conexión HTTP prolongada.

Trabajo:

- Reutilizar lo válido de los workers y del seguimiento de relectura actuales. Definir una máquina de estados para intentos, con transiciones atómicas, errores estructurados y fase/progreso persistidos. Traducir los estados públicos existentes durante la migración.
- Guardar una orden de ejecución en la misma transacción que confirma el plan. Un publicador reintentable entrega esas órdenes a Celery; confirmar el envío es idempotente. Esta tabla de salida cierra tanto la carrera pre-commit como la caída entre commit y publicación.
- Identificar la intención por archivo, revisión confirmada y clave de petición. Repetir un confirm por timeout devuelve el intento existente; no crea una importación nueva. Resolver también dos uploads idénticos todavía pendientes, sin eliminar la posibilidad de duplicación deliberada y explícita.
- Mantener tokens de propiedad, renovar leases mediante una conexión/transacción apropiada y verificar el token al publicar efectos. Una ejecución vencida no puede finalizar por encima de su reemplazo. Evitar un heartbeat que quede bloqueado por los locks que mantiene el mismo trabajo.
- Añadir recuperación de trabajos huérfanos, límites de reintentos, clasificación de errores y cancelación cooperativa antes de la publicación. Un usuario puede cerrar la pantalla y volver a consultar el mismo intento.
- Preparar filas y validar por lotes reanudables. Publicar ventas/gastos/stock en una transacción final acotada; los commits de preparación no son commits de negocio. Si el tamaño no cabe en ese presupuesto, definir y probar un mecanismo explícito de visibilidad por lote/ejecución antes de habilitarlo: no hacer commits parciales sobre tablas visibles.
- Acotar deduplicación al archivo/plan o a claves de operaciones candidatas usando índices y consultas por lotes. El consumo no debe crecer con todas las huellas históricas del tenant.
- Corregir y probar el vaciado del lote legacy. Mantener presupuestos absolutos de queries y pruebas de contención/reemplazo de identidades, además de las de crecimiento.
- Aplicar límites verificables a bytes, tamaño expandido, filas, columnas, hojas, celdas y tiempo de parsing. Inspeccionar protecciones existentes de MIME, rutas y acceso al original. No ejecutar macros ni fórmulas; un resultado de fórmula ausente/desactualizado requiere tratamiento explícito.
- Limpiar cargas huérfanas de almacenamiento mediante reconciliación de referencias. Los logs no deben volcar filas completas ni datos personales sin necesidad.

Aceptación: pruebas PostgreSQL + broker de prueba para muerte de worker, doble mensaje, fallo al publicar, pérdida de lease, reinicio y timeout del cliente. Una ejecución produce un único conjunto visible de efectos o ninguno. Memoria y consultas respetan presupuestos de F0. No se introduce una promesa de "exactly once" de transporte: la garantía reside en efectos idempotentes y publicación atómica.

**F6 — Relectura, deduplicación y reversión con el mismo contrato.** La relectura genera una nueva revisión del plan y compara efectos; no olvida las decisiones previas ni trata una relectura normal como una actualización automática del algoritmo.

Trabajo:

- Precargar las decisiones persistidas del archivo. Una reinterpretación con nuevas reglas se presenta como una revisión explícita con diferencias visibles. Los archivos anteriores sin plan necesitan un borrador revisable; las sugerencias aprendidas no reconstruyen automáticamente decisiones históricas inexistentes.
- Reconciliar altas, cambios y eliminaciones por identidad de origen y efecto. Conservar ediciones manuales, excluir hojas retiradas y mantener vínculos de proveedores/compras ajenos a la importación.
- Distinguir retry del mismo intento, relectura del mismo archivo, nueva versión de un archivo y cargas con períodos superpuestos. Para deduplicación entre archivos usar claves fuertes/comprobantes cuando existan; importe+fecha+nombre no basta para borrar una operación como duplicada. Casos ambiguos se presentan como candidatos, respetando la importación duplicada explícita.
- La posición física identifica la fila dentro de una versión del original; no alcanza para vincular filas entre versiones reordenadas. Definir ese vínculo con claves y evidencia de contenido, conservando multiplicidad: dos filas legítimas idénticas no deben colapsar por compartir hash. Una correspondencia ambigua requiere revisión.
- Investigar H12 con comparación de ledger y saldos antes/después. Eliminar ajustes redundantes mediante corrección del algoritmo; no deducir que cualquier movimiento adicional es incorrecto solo por su conteo.
- Añadir la prueba dedicada de rollback de `apply_reread`, con fallas inyectadas después de voids, altas de maestros, costos, vínculos y movimientos. Un fallo no deja medio estado sustituido.
- Completar el ledger necesario para revertir campos, maestros, vínculos, costos y stock. El borrado/undo debe distinguir reversión total, parcial por ediciones posteriores y caso histórico sin evidencia suficiente.

Aceptación: importar → releer igual → repetir conserva filas económicas y saldos; excluir una hoja retira solo sus efectos; editar manualmente y releer preserva esa edición; fallar a mitad revierte todo; borrar/undo no toca datos ajenos y no informa reversión completa cuando quedan efectos.

**F7 — Una experiencia de revisión coherente.** La interfaz se reorganiza alrededor del plan y sus decisiones; la extracción de componentes sigue responsabilidades, sin duplicar reglas financieras en TypeScript.

Trabajo:

- Un único estado de borrador y un único constructor del payload para carga inicial y relectura. Separar navegación de hojas, mapeos, problemas, impacto y estado del trabajo.
- Mostrar sugerido, recordado, confirmado e ignorado de manera diferenciada. La memoria de esquema debe guardar decisiones explícitas, no convertir una sugerencia precargada en aprendizaje confirmado por ausencia de edición.
- Mostrar problemas por fila/columna con ejemplos del original y del valor interpretado. Permitir corregir una regla de columna y recalcular sin editar fila por fila.
- Consumir requerimientos y capacidades del backend, incluida la explicación de campos obligatorios, costo/envío, inclusión de hojas y conflictos de identidad.
- Mostrar un resumen conciliado: filas consideradas, excluidas, ya aplicadas, pendientes y efectos a crear/cambiar/retirar; importes y stock afectados. No equiparar "filas leídas" con "filas importadas".
- Mantener navegación, accesibilidad, cancelación, recuperación tras desconexión y progreso real por fase. La finalización se comunica a partir del estado persistido, no de un callback compartido con cancelar.
- Preservar Otros como una bandeja operable con motivo, origen, corrección y aplicación idempotente por el mismo núcleo.

Aceptación: pruebas de navegador recorriendo carga → revisión → confirmación → cierre/reapertura → resultado → relectura → reversión. Mismos controles y decisiones producen el mismo payload en todos los recorridos. Una cancelación nunca muestra éxito de importación.

**F8 — Reparación histórica y habilitación gradual.** Corregir el software no repara automáticamente lo que ya fue importado.

Trabajo:

- Implementar diagnósticos de solo lectura para identificar candidatos por versión, flags efectivas, huellas de esquema, decisiones conocidas y contenido original: ignorados reaplicados, escalas numéricas, costos/envíos omitidos, ajustes redundantes y datos sin fecha. La falta de evidencia se registra como incertidumbre, no como autorización para corregir.
- Generar propuestas de reparación por archivo con antes/después, filas afectadas, importes, stock, conflictos y mecanismo de reversión. Nunca multiplicar todos los valores con punto por 1.000 ni releer todos los archivos con la heurística nueva.
- Probar las reparaciones sobre copias aisladas y muestras autorizadas; usar el mismo motor de planes y ledger. La aplicación productiva queda como una operación concreta posterior a revisar su impacto.
- Comparar planes nuevos y resultados esperados en modo de solo simulación. Los desvíos que sean correcciones conocidas deben estar aprobados como expectativas del corpus; no se exige imitar errores del motor anterior. Nunca escribir dos veces en negocio para comparar motores.
- Habilitar capacidades de forma gradual por tenant y formato, registrando la configuración efectiva. Cada plan confirmado queda ligado a su versión. El rollback de despliegue detiene nuevas ejecuciones incompatibles; los efectos ya publicados se revierten por ledger, no por cambiar una flag.
- Definir métricas y alertas: latencia por etapa, espera de cola, archivos sin progreso, retries, leases perdidos, filas pendientes por motivo, diferencias de conciliación, duplicados evitados, memoria y presupuestos SQL. Evitar etiquetas de alta cardinalidad con datos personales.
- Retirar ramas legacy y flags temporales cuando la ruta común supere las compuertas y los archivos históricos tengan una política de lectura. Actualizar los planes anteriores para que dejen de competir como hojas de ruta activas.

Aceptación: los escenarios del corpus pasan en los motores/formats habilitados; no quedan desvíos económicos inexplicados; recuperación y reversión se ensayan; se cumple el período de observación y los umbrales fijados antes del piloto. Cada dato histórico sospechoso queda reparado con evidencia o pendiente con una explicación, sin prometer reconstruir decisiones que nunca se guardaron.

**Orden de ejecución y entregas revisables.** La secuencia se ordena por dos criterios: qué desbloquea a qué, y **si la entrega puede salir sobre el motor actual o necesita primero el contrato común**. Las que lo necesitan no se comprometen hasta pasar la compuerta de E7a. El corpus arranca junto con E1: las reproducciones de E1–E3 se escriben con él.

| Entrega | Contenido | Motor actual / contrato común |
|---|---|---|
| E1 | Protección de estados del worker: adquisición atómica y escritura del resultado (H15) | Motor actual. Sin dependencias — es la primera. |
| E2 | `ignore` y relectura completos: resolución central consumida por todos los lectores, `context_entities` independiente, persistencia del borrador (H01/H02) | Motor actual. |
| E3 | Orden commit/publicación, transiciones y retry seguro (H05/H06) | Motor actual. Después de E1, nunca antes: no se agregan reintentos sin proteger primero las ejecuciones duplicadas. |
| E4 | Corpus ampliado y baseline + política de números, cantidades y fechas (F0/F2) | Motor actual. Empieza con E1. |
| E5 | Verificación operativa: Beat y configuración por servicio (H16/H10) | Motor actual. Independiente; puede correr en paralelo. |
| E6a | Costos, envíos, identidad y referencias: códigos externos, contradicciones, sentinelas, agrupamiento por comprobante, envío en tabla única (F4) | Motor actual. `_cobrar_envios_de_la_hoja` es hoy un closure del camino multihoja: extraerlo es refactor acotado, no contrato nuevo. |
| E6b | Relectura, Otros, deduplicación y reversión: rollback de `apply_reread` con fallas inyectadas, ledger de reversa, H12 (F6) | Motor actual. |
| E6c | Cola persistida, publicación atómica, leases y límites (F5) | Motor actual: una tabla de salida no depende de F3. |
| E7a | Contratos y persistencia del plan, migraciones, adaptadores CSV/XLSX y preview común (F3) | **Contrato común. Es la compuerta**: se compromete sólo si E1–E6 dejan una brecha medida. |
| E7b | Ruteos entre entidades tipados (los `cross` que hoy se descartan con warning), confirmación común y callers restantes, frontend unificado sobre el plan (F4/F7) | **Requiere E7a.** |
| E7c | Diagnósticos y reparación histórica; piloto, observación, retiro legacy y documentación (F8) | Los diagnósticos read-only son motor actual y pueden adelantarse; aplicar reparaciones usa el ledger de E6b; el retiro de ramas legacy depende de E7a/E7b. |

Cada entrega puede dividirse en PR más pequeñas. Las fronteras representan resultados comprobables; no se acepta una PR de traslado masivo de código mezclada con cambios de semántica.

**La compuerta antes del refactor grande.** 171 archivos de test tocan ingestión, relectura y mapeo, sobre 19.693 líneas en los cinco módulos centrales. Cada extracción o migración de E7a/E7b se justifica con una brecha medida que las correcciones localizadas no cierren. La paridad CSV/Excel es una prueba necesaria pero **no suficiente** de interpretación común: el objetivo es que preview, confirmación y relectura compartan reglas, y eso se mide también sobre relectura y reversión, no sólo sobre formatos equivalentes.

**Estrategia de verificación.** Se conserva la suite actual y se añaden pruebas que cruzan las fronteras donde aparecieron los defectos.

- Dominio: normalización, decisiones, identidad, costos y orden temporal. Pruebas de propiedades: permutar hojas/columnas conserva efectos cuando la semántica es la misma; `ignore` nunca alimenta un efecto; reintentar conserva estado; una contradicción nunca se resuelve por orden accidental.
- HTTP/archivo real: generar CSV/XLSX, pasar por parser, preview, confirm y consultar ventas, gastos, maestros y stock. No construir solo summaries ideales que el parser real nunca produce.
- PostgreSQL: índices, locks, dos sesiones concurrentes, fencing, rollback, lotes, migraciones y presupuestos SQL. No validar estas garantías únicamente con SQLite.
- Worker/broker: commit tardío, publicación fallida, doble entrega, muerte durante preparación/publicación, recuperación y agotamiento de retries. Para E1, el ciclo completo contra PostgreSQL real: dos workers adquiriendo a la vez (gana exactamente uno), mensaje repetido sobre un archivo terminado, un worker que perdió la propiedad intentando guardar, y escrituras de éxito **y de error** contra un estado posterior — un `FAILED` tardío sobre un archivo ya confirmado es tan corrupto como un `DONE`. Para el borrado hacen falta las dos ventanas: eliminado **antes** de reclamar (lo cubre el guard de la adquisición) y eliminado **después**, con el parseo en curso — ahí el token y el estado siguen siendo los del worker, así que sólo la condición en el `UPDATE` lo frena. Se prueba además el borrado ocurrido **entre** la lectura y la escritura, que es lo único que demuestra que la condición vive donde se escribe y no donde se lee.
- Para E3, contra PostgreSQL real: un error transitorio deja el archivo en `PENDING` (reencolable) y NO en `FAILED`; uno permanente cierra en `FAILED` sin gastar reintentos; la liberación respeta el fencing y no le saca el archivo al dueño actual ni revive uno eliminado. El orden commit→publicación se afirma en el endpoint, que es donde vive la causa.
- Escenarios a reproducir antes de afirmar nada sobre ellos: si el estado corrupto de H15 deriva en efectos económicos duplicados, con un barrido de **todos** los efectos (ventas, gastos, movimientos, maestros, vínculos, costos) tras un reparse forzado sobre un archivo ya confirmado — no sólo de las filas que llevan huella de dedup.
- Navegador: decisiones editadas/recordadas/ignoradas, formatos equivalentes, cancelación, reconexión, errores y relectura. Mantener las 237 pruebas revisadas como parte de la red, sin tomar su cantidad como objetivo.
- Rendimiento: repetir corpus y escalas comparables, con versiones y configuración registradas. Verificar tanto archivos mayores como tenants con mayor historial.
- Reparación: comprobar diferencias propuestas, preservación de ediciones manuales y reversión; no usar producción como entorno de prueba.

Las regresiones de integridad se verifican contra el comportamiento anterior para demostrar que detectan el defecto — y la mutación tiene que ser la del arreglo **a medias**, no sólo la del código viejo: en E1, quitarle al `WHERE` el token dejando el estado (y viceversa, y lo mismo con `deleted_at`) tiene que hacer fallar tests distintos, o el test no está probando la mitad que cree probar. Esto ya encontró dos huecos reales durante E1: una primera versión de los tests de propiedad que pasaba con el token quitado porque discriminaba por estado, y una condición de borrado que estaba sólo en la adquisición.

Regla que sale de eso, aplicable al resto del programa: **una condición que protege una escritura va en la sentencia que escribe**. Comprobarla al empezar el trabajo, o en una lectura previa, deja una ventana del tamaño del trabajo mismo. Se ejecutan los checks vigentes del repositorio y las suites afectadas; solo se amplía la ejecución cuando la fase cruza más módulos o deja riesgos sin cubrir.

**Criterio de finalización del programa.** Los defectos reproducidos de H01–H16 están corregidos con evidencia, y los escenarios marcados como "a reproducir" tienen una respuesta con método, no una atribución. Los casos históricos sin evidencia recuperable y los formatos expresamente fuera del alcance admitido están documentados como límites, sin usar esa categoría para cerrar bugs vigentes. Todas las entradas admitidas tienen una ruta definida; preview y ejecución comparten reglas; ninguna ambigüedad financiera se resuelve silenciosamente; los ciclos de relectura/retry/undo conservan integridad; los límites operativos están medidos; y existe un procedimiento probado de recuperación y reparación. El programa no se da por terminado por cantidad de tests, tamaño del refactor o cantidad de flags habilitadas.
