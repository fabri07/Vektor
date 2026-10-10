# Ingesta, socios, costos y margen: plan coordinado

Fecha: 2026-10-06. Estado: propuesta para revisión; no autoriza implementación, migración de datos ni cierre de PR.

## Objetivo y alcance

Completar B·3, preservar ediciones al cambiar las filas, implementar D·2–7 y portar B·4–9 del #48. Agregar identificación del costo de compra, margen por producto y venta, y margen promedio realizado en el dashboard. Reutilizar lo existente, con revisión independiente antes de cada merge.

Referencia: `docs/plans/cierre-pr48-y-b1.md`. Este documento precisa la ejecución y agrega costos/margen; no sustituye las decisiones ya tomadas allí. El estado de PR/CI debe verificarse al iniciar cada etapa. B1 del POS espera mediciones de hardware y `.venv312` espera conocer el motivo de la reversión anterior; quedan fuera.

## Evidencia del código local

- `build_incomplete_product` aún crea productos sin categoría. El merge no conserva los códigos externos en sus snapshots.
- La identidad de relectura depende de archivo, contexto y posición de fila. Conservar el fingerprint de una fila editada no preserva correctamente su correspondencia si cambia de posición.
- `purchase_cost.py` y `purchase_cost_decision.py` ya separan monto con ajustes incluidos, ajustes a aplicar, envío propio y envío compartido. Extenderlos, no crear otro motor.
- `FactsProvider` calcula costos de ventas con el costo actual del producto. Una compra posterior puede cambiar el margen mostrado de una venta pasada.
- `buildMarginBreakdown` promedia porcentajes del catálogo por categoría. Eso es margen teórico del catálogo, no margen realizado de las ventas.
- `deterministic_finance.calcular_margen_bruto` resta gastos totales a ventas. Unificar definiciones y consumidores (incluido chat) para no publicar márgenes distintos bajo el mismo nombre.
- D0 ya tiene protección para movimientos no comerciales del Libro Diario. No se encontró el modelo de capital de socios; el retiro todavía se registra como gasto.

## Coordinación agéntica y autorizaciones

El coordinador mantiene dependencias, contratos API, decisiones, matriz de pruebas y listado de PR. Hasta cuatro agentes simultáneos, incluido el coordinador:

1. Agente de backend/datos: dominio, persistencia, comandos, identidad y auditoría.
2. Agente de frontend: flujos y presentación, después de fijar el contrato.
3. Agente revisor independiente: busca pérdida de datos, errores monetarios, aislamiento entre tenants, concurrencia y problemas de reversión. En la fase de revisión se libera un slot si hace falta.

Cada PR tiene un responsable de implementación y un revisor que no sea su autor. Los agentes pueden investigar y preparar pruebas en paralelo; no editan simultáneamente el mismo archivo. `ingestion_import_service.py`, `reread_service.py`, Facts y las migraciones tienen un dueño por turno. Migraciones encadenadas al head real, sin generar múltiples heads.

Antes de cada etapa se presenta al dueño un plan concreto: comportamiento, archivos, contratos, decisiones pendientes, pruebas y riesgos. Se espera su OK antes de implementar esa etapa, empezando por B·3. No se repite una autorización ya concedida dentro de su alcance. La implementación produce un diff revisable; publicación, merge, cambios de producción y borrado de ramas requieren autorización aplicable.

Secuencia por etapa: especificación y ejemplos → contratos y diseño → implementación → revisión adversarial → correcciones y pruebas → documentación → resultado para aprobación. Aplicar las guías PostgreSQL antes de cambiar esquema, y revisión de seguridad para comandos, permisos y datos de usuarios. Para UI nueva, preparar el diseño antes de construirla.

## Orden y entregables

| Etapa | Entrega | Dependencias |
|---|---|---|
| 1 | B·3: categoría y códigos al fusionar | Estado actual de main |
| 2 | B·nuevo: identidad estable y conservación de ediciones | 1 y compuerta vigente |
| 3 | D·2–5: capital en dinero, retiros, Balance y onboarding | 2 para compatibilidad de procedencia |
| 4 | M·1–3: costo identificable y margen por venta/dashboard | 1–2; coordinar Facts con 3 |
| 5 | B·4–9: calidad, Otros, campos cruzados, códigos, alias y masivos | 2; cola de ventas integrada con 4 |
| 6 | D·6–7: especie e ingesta de aportes | 2–5 |
| 7 | Cierre de #48 y su rama | Matriz de portado completa, revisión y autorización |

Costos/margen se agrega antes de la cola con alias para que resolver una venta pendiente actualice un único cálculo. Investigación y diseño pueden avanzar en paralelo; las escrituras compartidas y merges respetan estas dependencias.

## 1. B·3 — categoría inferida y código externo

- Producto incompleto: inferir categoría con el rubro y política existente. Confianza alta permite completar lo vacío; media solo sugiere; baja no asigna. Nunca reemplazar categoría manual ni dejar de exigir los campos que faltan para completar el producto.
- Merge: conservar código y fuente del descartado si el ganador carece de código. Si ambos tienen códigos, mantener el ganador y registrar el descartado/conflicto en auditoría; no inventar un segundo código canónico.
- Normalización, liberación del código del descartado y transferencia dentro de una transacción, respetando el índice único por tenant. Snapshot antes/después, undo y chequeo de modificaciones posteriores deben incluir código y fuente.
- Aceptación: inferencia alta/ambigua/sin rubro; categorías manuales; ganador con/sin código; colisión; rollback; undo y carrera PostgreSQL. Stock y precios no cambian por esta entrega.
- Entrega: PR B·3 con test de regresión significativo y actualización de `docs/claude/ingesta.md`.

## 2. B·nuevo — identidad independiente de posición

- Separar identidad lógica de la fila y localizador de procedencia. Conservar hoja/fila para explicar el origen; no usarlas como única identidad.
- Prioridad: identificador explícito del origen dentro de su contexto. Si no existe, correspondencia conservadora basada en el contenido original guardado y señales estables. No derivarla del registro ya editado ni considerar un hash de toda la fila una solución universal.
- Diseñar correspondencia uno a uno: filas idénticas, cambios de monto, encabezados o contexto no permiten elegir arbitrariamente. Si la evidencia no alcanza, mostrar conflicto y conservar la edición sin aplicar una reconciliación ambigua.
- Mantener idempotencia de importación, historial, ledger y compuerta de pérdidas dentro de la transacción. Compatibilidad para registros anteriores sin identidad nueva: migración de metadatos solo donde pueda probarse, y revisión en los demás casos.
- No agregar identidad transversal de clientes/productos descartada por F-ID: esta identidad es de filas de archivo.
- Aceptación: reordenar, insertar y eliminar filas; dos filas iguales; origen corregido; edición de importe/categoría/producto; contexto totalmente editado; cambios de parser; dos relecturas; undo/F11; stock; concurrencia y rollback íntegro cuando no se acepta una pérdida.
- Entrega: diseño de identidad revisado antes del PR. La compuerta se conserva; no se trata como sustituto de este arreglo.

## 3. D·2–5 — aportes y retiros en dinero

Separar en cuatro PR para hacer visible cada efecto:

1. Modelo `owner_capital_movements`, comandos auditados y carga manual. Tipos: aporte, retiro, préstamo de socio y devolución. Fecha de negocio, socio opcional, importe positivo, efectivo/banco, procedencia, anulación, idempotencia y aislamiento por tenant. Mantener PIN del retiro. Definir compatibilidad del endpoint anterior y de su respuesta, no solo su URL.
2. Reparación de retiros existentes: script dry-run por tenant/período, solo registros marcados `profit_withdrawal`, enlace al gasto original, reejecución sin duplicados y reversión. No ejecutar la reparación dentro de Alembic. Presentar antes/después de gastos, margen y score; aplicación en producción como paso separado autorizado.
3. Facts y Balance analítico: aportes/retiros por socio, dinero/especie, préstamos pendientes y flujo operativo separado del flujo con capital. No confundir saldo explicado con arqueo contado. Aportes y retiros nuevos no modifican ventas ni margen; la reparación sí elimina el efecto incorrecto de los retiros antiguos.
4. Onboarding: capital inicial opcional, declarado y fechado; evitar duplicarlo con el saldo actual de caja o con un aporte ya registrado.

Préstamos ambiguos o bancarios no se convierten en aportes de socios. Mantener el alcance analítico: no se ofrece un balance contable certificado.

Aceptación: aporte $100.000 y retiro $10.000 producen flujo de capital $90.000, sin cambios en ventas, gastos operativos ni margen. Verificar préstamos/devoluciones, compatibilidad de clientes anteriores, PIN, permisos, reejecución y reversión de la reparación.

## 4. M — costo y margen automático

### M·1: costo identificable de compra y producto

Reutilizar los motores existentes. Distinguir valor unitario de total de línea y cantidad en unidad de venta; productos por peso usan el factor de conversión vigente. Clasificar columnas de costo/precio/envío/impuestos/descuento con confianza y procedencia. Una sugerencia ambigua se confirma; nunca tomar precio de venta como costo automáticamente.

Cada costo indica: monto original, cantidad, descuento, impuestos ya incluidos o a aplicar, envío ya incluido o adicional, reparto compartido, moneda/unidad, fecha, origen y estado confirmado/desconocido. No equiparar desconocido con cero o con «sin envío».

Definir antes de implementar la política de impuestos para el negocio: costo efectivamente pagado vs costo sin impuestos recuperables. La inclusión de impuestos es independiente de que el monto los traiga incluidos. No deducir recuperabilidad del nombre «IVA». La venta y su costo deben usar bases compatibles; si no puede normalizarse la base, no presentar un porcentaje comparable como definitivo.

Fórmulas sobre valores confirmados:

`base = importe de mercadería - descuento no incluido + impuestos aplicables no incluidos`

`costo de línea = base + envío adicional de línea al costo + envío compartido asignado`

`costo unitario = costo de línea / cantidad en unidades de venta`

No sumar componentes que ya estén incluidos. Mantener envío de línea y compartido separados. El compartido se reparte solo entre líneas del mismo comprobante y con decisión explícita; la suma debe cerrar al centavo. Evitar descontarlo de resultado otra vez como gasto cuando ya integra el costo. La salida de caja sigue representando el pago real.

Si la política aprobada excluye impuestos recuperables que ya están incluidos, extraer/restar únicamente su importe confirmado; normalizar también el ingreso de venta a esa base. Sin desglose o tasa/base confirmadas, no reconstruirlo por adivinación.

Si faltan cantidad o costo verificable, mantener pendiente y explicar qué falta. En «Otros» de compra mostrar el desglose y permitir resolver producto/base/cantidad; al confirmar usar el mismo comando de costos que la ingesta normal y la compra manual.

Costo cero solo es conocido con declaración explícita; el cero de un producto incompleto no prueba costo gratis. El costo de referencia del catálogo conserva su política actual hasta aprobar un cambio. Un costo promedio ponderado de inventario es otro cambio de valuación y no se introduce implícitamente por pedir un promedio en dashboard.

### M·2: margen por producto y venta

- Catálogo: margen estimado con precio de venta actual y costo de referencia, con desglose y base. No confundir margen con recargo sobre costo.
- Venta: persistir costo aplicado por línea y su procedencia/fecha/base al registrar la operación, usando la fuente histórica que corresponda. Revisar `InventoryMovement.unit_cost` antes de agregar campos redundantes; cubrir también ventas sin efecto de stock.
- Antes de M·2 aprobar la regla concreta de selección de costo cuando haya varias compras. El costo de referencia vigente puede usarse como referencia congelada y declarada, sin prometer valuación de inventario. Una venta capturada offline guarda esa referencia al capturar; un reintento tras perder la respuesta conserva identificador, payload y snapshot, aunque entretanto cambie el costo del producto.
- Margen bruto en pesos: ingreso de venta después de descuentos, sobre base impositiva comparable, menos costo de unidades vendidas. Margen porcentual: `margen en pesos / ingreso × 100`, solo si ingreso > 0.
- Venta sin costo conocido: «Costo pendiente», margen desconocido. Nunca 100% por defecto. Conservar margen negativo si vender por debajo del costo es real.
- Mostrar margen/costo en detalle o listado de ventas según permisos; no en el ticket del cliente. «Otros» de venta muestra margen pendiente hasta resolver identidad y costo.
- Resolver producto mediante alias no mueve stock. Si reconstruye costo de una venta antigua, usar evidencia histórica y auditar la corrección; no convertir el costo actual en histórico silenciosamente. Si no hay evidencia suficiente, marcar estimación o mantener pendiente.
- Cambiar el costo actual no altera snapshots de ventas anteriores. Importación, venta manual y POS usan el mismo servicio. Anulación, undo y relectura actualizan el agregado desde las ventas vigentes, sin duplicar costos.
- Para ventas antiguas, plan de diagnóstico/backfill con procedencia y cobertura. No rellenar automáticamente con costo actual y etiquetarlo como costo realizado histórico.

### M·3: promedio en dashboard

Mostrar «Margen bruto de ventas» del período seleccionado, ponderado por ingreso vendido:

`100 × (suma de ingresos con costo conocido − suma de costos asociados) / suma de ingresos con costo conocido`

Mostrar también ganancia bruta conocida en pesos y cobertura: `ingresos con costo conocido / ingresos totales`. Con cobertura parcial, indicar «sobre ventas con costo conocido»; no presentarlo como margen definitivo del negocio. Sin ventas elegibles/costos, mostrar «Sin datos suficientes».

No promediar porcentajes de productos ni usar compras del período como si fueran costos de lo vendido. Mantener, si se conserva, el indicador del catálogo con nombre «Margen estimado del catálogo». Un proxy por gastos COGS solo puede seguir como estimación distinta, nunca mezclado con margen realizado. Alinear Facts, finanzas determinísticas, API y chat con las mismas definiciones; corregir o renombrar los consumidores que hoy llaman bruto al resultado después de gastos totales.

Excluir anuladas, demo y movimientos de capital según los filtros vigentes. Ventas mixtas no se duplican al repartir pagos. Márgenes por producto/categoría siguen la misma fórmula y período. Separar margen bruto de resultado neto y de flujo de caja.

Actualizar también el gráfico histórico y sus consumidores; tarjeta, serie temporal y detalle deben conciliar en un mismo período y filtro.

Ejemplo de aceptación: ventas $100 con costo $80 y $900 con costo $450 → ganancia $470 y margen ponderado 47%; no 35%. Compra: 10 unidades, base $1.000, envío adicional $100 e impuesto aplicable adicional $210 → costo unitario $131. Venta de una unidad a $200 sobre la misma base → ganancia $69, margen 34,5%. Si el importe ya incluye esos componentes, no se suman de nuevo.

Pruebas: combinaciones con/sin envío y con/sin impuestos, incluido/adicional/desconocido, recuperabilidad confirmada, descuentos, flete compartido repetido, centavos, unidades por peso, falta de costo, negativos reales, cero declarado, compra posterior, ventas antiguas, alias, anulación y relectura. Comparar detalle, listado y dashboard contra el mismo conjunto de datos.

## 5. B·4–9 — portado por PR, con contratos armonizados

- B·4: completar solo vacíos, respetar edición manual; detectar Total/Subtotal conservadoramente. «Total Clean» y filas descriptivas siguen siendo datos.
- B·5: «Otros» por archivo y motivo, con cantidades del grupo completo. Descarte con preview y confirmación, no limitado a la página visible. Aprovechar la resolución individual existente, incluidos costos de compra/venta de M.
- B·6: campos cruzados, una vez por entidad y solo vacíos; ledger y reversión F11 con protección de edición. Decidir reutilización de `UPDATE_*` antes de agregar acciones o migraciones.
- B·7: vínculo por código de cliente/proveedor, normalizado, único y del tenant. Código desconocido no crea un maestro: resolución actual y aviso.
- B·8a: backend de ventas sin producto y alias aprendidos, tabla propia con procedencia. Alias débil después de barcode/SKU; conflicto no vincula; no participa del lector POS. Merge mueve aliases; F11 no borra aprendizaje manual. Vincular actualiza producto y cálculo M, sin stock implícito.
- B·8b: pantalla de cola, búsqueda, conflicto, costo/margen pendiente y resultado de la vinculación. Se implementa sobre contrato aprobado de 8a.
- B·9: masivos «Guardar como campo propio» e «Ignorar vacías». No aceptar ambigüedades masivamente ni saltar las compuertas de riesgo.

Cada PR verifica permisos, tenant, idempotencia, reversión, compatibilidad con frontend anterior y ausencia de pérdidas.

## 6. D·6–7 — especie e ingesta

- Mercadería: aporte valorizado y stock `opening_balance` vinculados, en una transacción. Sin salida de caja ni gasto por aportarla; costo de lo vendido sí aparece en M/COGS.
- Equipamiento: valor declarado informativo, sin amortización automática ni margen.
- Ingesta: destinos para libro mixto, Libro Diario y hojas sueltas; aportes inequívocos con confirmación. Préstamos sin socio identificable siguen en «Otros».
- Aplicar identidad estable, procedencia, ledger y F11 también a capital. Si se anula aporte de mercadería cuyo stock ya se vendió, definir y validar la reversión compatible con inventario, no borrar el vínculo arbitrariamente.
- Aceptación: confirmar/reimportar/releer no duplica capital ni stock; anular/borrar archivo preserva ediciones y concilia Balance, inventario y margen. El aporte no es facturación.

## Validación y revisión de código

- Pruebas unitarias de reglas monetarias e identidad; integración de comandos, ledger y permisos; HTTP para contratos; PostgreSQL para unicidad, locks, RLS, migraciones y rollback. Frontend con pruebas de flujos y contratos pertinentes.
- Ejecutar checks exigidos por el repositorio y CI; no considerar SQLite sustituto de PostgreSQL. Una mutación dirigida por protección crítica comprueba que el test detecta la pérdida o doble conteo.
- Revisor independiente revisa diffs y escenarios adversos: identidad ambigua, importe incluido dos veces, promedio engañoso, stock repetido, pérdida de código, modificación posterior y clientes anteriores.
- No fusionar con hallazgos críticos abiertos. Registrar decisiones de menor severidad y su resolución. Documentación de ingesta/finanzas/costos junto al cambio correspondiente.
- Ensayo sobre fixture sintético y ASTERIA disponible: filas originales/reordenadas, movimientos mixtos, compras con desglose y resumen derivado. Conciliar cantidades, capital, costos y margen; no duplicar resúmenes. Pruebas en cuentas reales se planifican y autorizan separadamente.

## 7. Cierre

Mantener matriz de #48: commit/función original → portado en PR → prueba → descartado con motivo. Costos/margen y capital nuevo se identifican como alcance adicional, no se atribuyen al portado antiguo.

Cerrar #48 solo cuando lo comprometido esté integrado y verificado; listar todos los PR y pendientes fuera del alcance. Borrar únicamente su rama después de verificar que no quedan commits o trabajo útiles sin portar y obtener la autorización aplicable. No borrar `spike/pos-b1` mientras falten las mediciones.

## Primera entrega a aprobar: B·3

Al iniciar: actualizar evidencia contra main y presentar plan limitado a inferencia de categoría y transferencia/auditoría de código y fuente, incluyendo índice único, undo, concurrencia PostgreSQL y ausencia de cambios en stock/precio. Este documento no inicia su implementación.
