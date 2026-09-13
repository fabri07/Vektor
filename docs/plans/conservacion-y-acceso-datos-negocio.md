# Conservación y acceso a los datos de cada negocio

Estado: propuesta para implementar; este documento no cambia código ni datos.
Fecha: 2026-09-12.

## Objetivo y definición de cierre

Cerrar el circuito **archivo → interpretación → persistencia → consulta → exportación** para Productos, Ventas, Gastos, Clientes y Proveedores. Otros conserva su función de revisión, con acceso a los originales y motivos existentes.

El resultado debe servir para cualquier cuenta y rubro. Asteria es un caso de verificación, no una condición en el código.

**Contrato de cierre:** cada columna del archivo confirmado tiene un destino consultable: campo de negocio, vínculo, dato adicional, transformación documentada, pendiente de revisión o exclusión explicada. Todo valor de negocio guardado puede consultarse y exportarse con los permisos correspondientes. La tabla puede ocultarlo; no puede hacerlo desaparecer.

Conservar, mostrar, editar y usar para calcular son permisos y decisiones diferentes. Preservar una columna desconocida no autoriza a sumarla a costos, stock ni métricas.

## Alcance acotado

- Reutilizar `custom_fields`, las definiciones de campos, `SmartTable`, las categorías propias, las decisiones de mapeo, el historial de importaciones y la descarga del archivo original.
- Mantener campos estrictos para montos, fechas de negocio, stock, identidades y relaciones.
- Incorporar datos particulares del negocio como campos adicionales con significado y tipo explícitos cuando se conozcan; texto si no se conocen.
- Respetar hojas y columnas excluidas: no convertirlas automáticamente en entidades ni enviarlas a Otros contra la decisión del usuario. El resultado explica su exclusión y permite consultar el archivo original mientras exista.
- No introducir tablas SQL por cliente, un motor de formularios, EAV, ML, un rediseño visual general ni otro pipeline de importación.
- No incluir optimización de `external_code`, ampliación de vocabularios ni nuevos cálculos financieros. Son cambios independientes.
- No prometer conservación indefinida: la baja del archivo y las reglas de retención existentes siguen aplicando. Indicar cuándo un original ya no está disponible.

## Base verificada en el repositorio

| Pieza existente | Qué aprovechar / brecha observada |
|---|---|
| `backend/app/application/services/field_definition_service.py` | Combina campos del rubro y del tenant; `ensure_custom_field_exists` registra campos elegidos al confirmar. Los valores en JSON pueden existir sin definición. |
| `backend/app/persistence/models/field_definitions.py` | Tiene etiquetas, tipos, orden, habilitación y cambios por cuenta. No crear otro registro paralelo de campos personalizados. |
| `frontend/src/lib/customFields.ts` y `customFieldsEditable.tsx` | Construyen columnas desde definiciones, pero excluyen `is_base_field`, suponiendo que cada página ya implementó todos los campos base. |
| `frontend/src/components/ui/SmartTable.tsx` | Selector y búsqueda sobre columnas declaradas; preferencias en memoria; CSV de las columnas visibles y filas cargadas. |
| Servicios frontend de productos, ventas, gastos y proveedores | Tienen límites de acumulación de páginas. Un CSV construido desde ese arreglo no garantiza exportación completa del negocio. |
| `backend/app/api/v1/ingestion.py` | Confirma con transacción y contadores; compacta el resumen quitando filas y contextos completos. El comprobante nuevo debe persistirse antes de esa compactación. |
| `ingestion_schema_decision_service.py` | Recuerda decisiones explícitas por cuenta y esquema. Reutilizarlo; no convertir sugerencias automáticas en elecciones del usuario. |
| `tenant_categories_service.py` y API de productos | Ya existen categorías propias por cuenta. Conectar la importación y consulta con ese mecanismo. |
| `backend/app/api/v1/files.py` | Ya existe descarga autorizada del original mediante URL temporal. |

Hay modificaciones locales previas en categorización, sus tests y las columnas/tipos de Productos. Integrarlas como base sin sobrescribirlas. Los resultados de Neon y las suites informados en la conversación no se consideran verificaciones realizadas por este plan.

## Reglas que se deben cumplir

1. Una columna con pocos valores no se elimina automáticamente. Tampoco se confunde `0` o `false` con ausencia.
2. Una columna elegida por el usuario debe tener destino ejecutable o una alternativa explícita antes de confirmar. Un flag apagado no puede cambiar “Proveedor” por “Marca”.
3. Los campos adicionales no afectan FactsService, costos, caja ni stock por el solo hecho de existir.
4. No convertir códigos en números ni truncar valores para hacerlos encajar. Los valores incompatibles conservan su original y un motivo de revisión.
5. El nombre visible no es la identidad del campo. Renombrar no mueve datos; columnas distintas no se fusionan por compartir una etiqueta normalizada.
6. Los vínculos tienen cardinalidad real: un producto puede tener varios proveedores. Mostrar una lista o un contador con detalle; no elegir uno arbitrariamente.
7. El acceso depende del tenant autenticado y permisos de la sección. No exponer indiscriminadamente el JSON interno, claves técnicas ni campos de otra cuenta.
8. Confirmación, relectura y reversión mantienen sus garantías actuales. Mostrar datos existentes no requiere reimportar ni recalcular movimientos.

## Cambio 1 — Un catálogo de lectura que cubra los campos guardados

### Implementación

Extender el módulo de campos con una proyección de lectura por entidad, sin cambiar el contrato de edición existente. Propuesta de endpoint: `GET /fields/available?entity_type=product`, con entidades permitidas explícitamente.

Cada descriptor contiene:

- `field_id`: identidad estable, incluyendo entidad y origen; independiente de la etiqueta.
- `label`, `data_type` y unidad cuando exista (ARS, porcentaje, unidades).
- `origin`: canónico, adicional declarado, evidencia de negocio o adicional histórico recuperado.
- `value_path`: ruta permitida a un atributo o clave de `custom_fields`; nunca código ejecutable.
- `editable`, `exportable`, `searchable`, `default_visible` y regla financiera, si ya existe.

Componer los descriptores con las definiciones actuales y un registro explícito de campos canónicos/evidencias de negocio. Reutilizar etiquetas y tipos actuales; no copiar tres catálogos divergentes para importación, tabla y exportación.

Registrar expresamente `unit_cost_ars`, `list_price_ars`, `purchase_base_cost` y `shipping_percentage`, entre los campos ya soportados. Unificar su identidad aunque hoy se accedan por claves de columna sintéticas. Evitar columnas duplicadas al combinar las nuevas columnas locales de Productos con campos adicionales.

Para nuevas importaciones, asegurar en la misma transacción la definición de cada campo adicional efectivamente guardado. Las columnas no reconocidas de una hoja incluida se presentan como “Conservar como dato adicional”; la confirmación debe reflejar esa decisión, no hacer una asignación financiera implícita. Reutilizar el tratamiento existente de colisiones de claves y de entidades no resueltas.

### Información histórica sin definición

- Detectar claves de negocio guardadas sin descriptor mediante una revisión paginada por tenant y entidad; no limitarse a la primera página de la tabla.
- Distinguir claves de negocio de las técnicas mediante el registro de claves reservadas y el mapeo histórico cuando esté disponible. Las claves ambiguas quedan en una lista de revisión autorizada, no se publican automáticamente como editables.
- Crear definiciones faltantes de forma idempotente, por lote, sin renombrar campos existentes, reactivar campos deshabilitados deliberadamente ni modificar sus valores.
- Las evidencias y recuperaciones históricas empiezan como solo lectura. Habilitar edición requiere una semántica y un endpoint que ya la soporten.
- Primero producir un listado de cambios; luego aplicar únicamente el registro de metadatos. No volver a importar transacciones para recuperar visibilidad.

**Aceptación:** un campo adicional con un único valor aparece como disponible; los campos base y evidencias también; no hay duplicados ni claves internas expuestas. La segunda ejecución de recuperación no crea nuevas definiciones.

## Cambio 2 — Consulta completa y vistas personales en las cinco secciones

### Implementación

- Construir las columnas desde el catálogo de lectura, conservando los renderizadores específicos actuales para campos que los necesitan. Sustituir el descarte general de `is_base_field` por deduplicación mediante identidad estable.
- Incorporar un detalle compartido “Todos los datos”, accesible desde cada registro. Mostrar campos canónicos y adicionales, su tipo/unidad y procedencia disponible; indicar “procedencia no disponible” para históricos sin evidencia suficiente.
- Mostrar valores no interpretados como originales, no como guiones o ceros. Renderizar objetos/listas de forma legible y segura, con expansión si son grandes.
- Permitir ocultar/mostrar, reordenar y restablecer columnas. Para este alcance, persistir preferencias locales bajo una clave versionada por tenant + usuario + sección. Son preferencias de este navegador; no prometer sincronización entre dispositivos.
- Los campos elegidos explícitamente en una importación se señalan como nuevos en el selector. No se pisan elecciones manuales previas ni se abre una tabla con cientos de columnas.
- Usar una vista inicial pequeña de campos canónicos y dejar los demás disponibles. No implementar un motor de porcentajes de cobertura para decidir visibilidad; Asteria no define los defaults de todas las cuentas.
- Campos deshabilitados siguen siendo consultables en el detalle histórico y exportables si se conservan y el usuario tiene permiso; deshabilitar para nuevas cargas no equivale a borrar el pasado.
- La búsqueda actual abarca las columnas declaradas de los registros cargados. Mostrar el alcance y límites de carga; no anunciar búsqueda global si no se consulta todo el conjunto.

**Aceptación:** un dato oculto se encuentra en el detalle y selector; las preferencias sobreviven a la navegación y recarga; cambiar de usuario/cuenta no hereda la vista anterior; cero, falso y texto inválido siguen siendo distinguibles.

## Cambio 3 — Exportación sin pérdidas por visibilidad o paginación

### Implementación

Ofrecer dos acciones con alcance explícito:

1. **Exportar vista actual:** columnas visibles y registros cargados que cumplen los filtros/búsqueda de esa vista. Indicar cuando la lista cargada es parcial.
2. **Exportar todos los campos:** todos los campos de negocio autorizados y todos los registros del alcance seleccionado, aunque las columnas estén ocultas o las filas no estén cargadas en el navegador.

Para la segunda, usar exportación del lado servidor con recorrido paginado estable, apoyado en los repositorios y filtros existentes. Reutilizar infraestructura de exportación si existe al implementar; no crear un sistema de trabajos asíncronos para este cambio. Selección de entidad y campos mediante allowlist, nunca interpolación de nombres SQL del cliente.

Compartir los descriptores de lectura. Mantener números en representación exacta, fechas inequívocas y códigos como texto, sin usar la presentación monetaria redondeada de la tabla como valor exportado. Conservar el escape CSV y comprobar el tratamiento de celdas que una planilla podría ejecutar como fórmula. Ofrecer la descarga del original para fidelidad exacta del documento fuente; CSV no preserva fórmulas, formato y tipos de un Excel completo.

El alcance de filtros y búsqueda debe ser visible: si no se soporta una búsqueda local en el servidor, no fingir que se aplicó. La descarga completa no usa `MAX_PAGES` del frontend. Definir una lectura consistente para que escrituras concurrentes no dupliquen ni salteen filas durante una exportación.

**Aceptación:** un conjunto de más de 5.000 registros se exporta completo; una columna oculta y una presente solo en una página tardía se incluyen; los códigos con ceros iniciales y decimales no se alteran en la representación emitida; otra cuenta no puede descargarlos.

## Cambio 4 — Comprobante de importación y capacidades reales

### Implementación

Persistir un comprobante compacto y versionado por ejecución, antes de eliminar los contextos del resumen. Extender el resultado existente de confirmación/intento y el resultado de relectura; no persistir otra copia de todas las filas del archivo en ese JSON.

Por contexto y columna, guardar:

- Identidad de origen (archivo, ejecución, hoja/contexto y posición/nombre original de columna).
- Destino elegido y descriptor de campo/vínculo resultante, cuando corresponda.
- Resultado efectivo: guardado, transformado, pendiente, excluido o mixto; motivo legible.
- Conteos de valores/filas por resultado cuando el importador puede acreditarlos, y referencias a registros de revisión o al original.
- Distinción entre exclusión por decisión del usuario y exclusión por regla del sistema, por ejemplo una hoja de totales derivados.

Los conteos vienen de la ejecución efectiva, no del preview ni de restar totales de entidades: varias filas pueden actualizar un producto. No sumar contadores de columnas como si fueran filas distintas. Cuando el desglose no esté disponible, indicarlo sin inventar cifras.

Exponer el comprobante desde el historial del archivo y enlazarlo desde la importación terminada. Las relecturas generan una nueva ejecución identificable; la reversión marca la ejecución revertida sin borrar su explicación. Mantener una referencia autorizada al archivo original mientras exista.

Para ejecuciones antiguas, reconstruir solo lo demostrable desde resultados y mapeos guardados. Mostrar “detalle histórico no registrado” donde falte evidencia. No afirmar que una fila se importó porque hoy exista una entidad parecida.

Antes de confirmar, el mapeador obtiene las capacidades efectivas del tenant por una respuesta separada del catálogo estático. Deshabilitar destinos no ejecutables y explicar qué habilitación falta. Si llega un payload antiguo que pide un vínculo no habilitado, rechazar esa decisión con mensaje accionable antes de escribir, permitiendo que el usuario elija explícitamente conservar el texto como adicional. Mantener la verificación existente de capacidades entre API y worker.

**Aceptación:** se puede explicar el destino de cada columna sin consultar logs; una columna con resultados parciales no figura como guardada íntegramente; `supplier:name` nunca se transforma silenciosamente en marca; fallo de importación no deja un comprobante de éxito.

## Cambio 5 — Categorías propias y verificación operativa de Asteria

### Categorías

- Reutilizar `tenant_categories_service.py` y el endpoint actual de categorías propias.
- Conservar la etiqueta original como dato de negocio trazable. Resolver primero una categoría propia explícitamente elegida o existente en esa cuenta; después aplicar las reglas actuales cuando corresponda.
- Si no hay correspondencia, ofrecer conservar/crear la categoría propia mediante la confirmación existente. No crear categorías basadas en cada palabra del nombre del producto.
- Mantener separada la categoría del negocio de cualquier equivalencia canónica empleada en análisis. Una categoría propia no modifica reglas financieras ni hereda un significado por semejanza del nombre.
- Reutilizar decisiones explícitas recordadas por cuenta y esquema; no agregar un sistema nuevo de aprendizaje ni modificar globalmente el vocabulario con correcciones de un solo negocio.

**Aceptación:** “Accesorios” se conserva y puede utilizarse en una cuenta aunque el catálogo del rubro no la contemple. Otra cuenta no hereda esa elección; el original continúa consultable.

### Asteria como comprobación, no como implementación especial

1. Verificar las capacidades efectivas en API y worker y el estado de sus importaciones. No dar por verificado el flag a partir del diagnóstico pegado en la conversación.
2. Comprobar que costo, precio de lista, precio sin envío y porcentaje de envío están disponibles por el mecanismo común, sin reimportar para mostrarlos.
3. Si se habilita Producto↔Proveedor, aplicar primero el flujo completo en un tenant de prueba y después una relectura controlada de Asteria bajo las capacidades correctas. Inspeccionar previamente el plan de relectura y sus efectos; la operación no forma parte de redactar este documento.
4. Distinguir vínculo declarado de compra real. “Ya tenía la mercadería” no genera gastos COGS ni salida de caja por habilitar proveedores. Un gasto puede existir sin proveedor identificado.
5. Comprobar consulta en ambos sentidos del vínculo y representación de múltiples proveedores. No prometer que gastos/ventas sin referencias suficientes quedarán vinculados automáticamente.

## Orden y forma de entrega

Implementar los cinco cambios en el orden anterior, con commits revisables por comportamiento. Los cambios 2 y 3 consumen el contrato del 1. El 4 necesita registrar el destino efectivo del 1 y los resultados existentes del importador. El cierre del 5 verifica el conjunto.

Cada entrega debe incluir código, documentación del comportamiento y pruebas correspondientes. No reescribir páginas completas para agregar columnas ni reformatear masivamente el backend. No mezclar estos commits con los cambios locales anteriores sin revisar el diff.

La proyección de campos, las preferencias locales y el comprobante compacto se plantean sobre estructuras existentes; no requieren por diseño una migración nueva. Si aparece una necesidad de esquema adicional, justificarla con el dato concreto que no se puede representar, en vez de abrir una reforma general.

Este alcance es mayor que agregar cuatro columnas: incluye garantía de acceso histórico y exportación completa. No estimar horas ni declararlo terminado por tener tests de columnas en verde; usar los siguientes criterios de cierre.

## Verificación y aceptación final

| Caso | Resultado exigido |
|---|---|
| Columna desconocida con un valor entre muchas filas vacías | Conservada como adicional tras confirmar; visible en detalle/selector y exportable. |
| Columna completamente vacía | No genera datos ficticios; su destino o exclusión queda explicado. |
| `0`, `false`, código `0007`, decimal preciso y texto que no cumple el tipo | Se distinguen de nulos; no se recortan ni se reinterpretan silenciosamente. |
| Campo guardado sin definición / campo base no implementado en una página | Accesible mediante recuperación o descriptor; sin duplicar valores ni columnas. |
| Hoja y columna excluidas; hoja derivada | No crean entidades ni Otros automáticamente; comprobante con motivo y acceso al original disponible. |
| Costo base + envío + costo final | Todos consultables; ninguna suma nueva ni doble contabilización. |
| Relación proveedor no habilitada / cambio de capacidad antes de ejecutar | Decisión bloqueada de forma explicable; sin conversión silenciosa ni ejecución con capacidades distintas. |
| Categoría propia | Original y selección conservados; decisiones aisladas por tenant. |
| Exportación de más de 5.000 filas | Conjunto completo del alcance declarado, con columnas ocultas y de páginas tardías. |
| Reimportación, relectura y reversión | Sin duplicados de definiciones o movimientos; resultados por ejecución coherentes; restauración de valores acorde al mecanismo existente. |
| Históricos sin trazabilidad suficiente | Se muestra la limitación; no se fabrica un historial. |
| Cambio de cuenta, permisos y preferencias | Sin mezcla de datos, descriptores privados ni vistas entre cuentas/usuarios. |

Ejecutar las suites existentes de campos, importación, categorías, reversión y tablas/exportación afectadas. Agregar pruebas sobre estos límites y contratos, no sobre detalles triviales de implementación. Usar PostgreSQL para las garantías que dependan de JSONB, concurrencia o paginación transaccional; la prueba funcional puede empezar con fixtures de negocios sintéticos.

Validar lint y tipos en ambos lados. Ejecutar checks adicionales solo cuando el cambio o un fallo lo justifique. Los tests y mediciones de conversaciones anteriores no sustituyen correr las verificaciones del código que se entregue.

**Demostración final:** dos negocios de rubros distintos importan una columna propia poco poblada, una categoría propia y un campo conocido que estaba oculto. Cada uno puede consultar y exportar sus datos, explicar el destino de todas las columnas y conservar sus preferencias. Las métricas financieras solo cambian por los campos y operaciones autorizados para ello. Después se comprueba Asteria con el mismo mecanismo.
