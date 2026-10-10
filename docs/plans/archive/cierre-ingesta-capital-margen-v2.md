# Cierre de ingesta, capital y margen — plan consolidado v2

Fecha: 2026-10-07. Propuesta para aprobar; no inicia implementación, merge ni reparación de producción. Consolida `ingesta-socios-costos-margen-coordinado.md` y el plan aportado por el dueño. Conserva los planes anteriores como referencias, pero este define el orden propuesto para el trabajo pendiente.

## Qué conserva y qué corrige

| Tema | Plan coordinado anterior | Plan aportado | Decisión consolidada |
|---|---|---|---|
| Ejecución | Etapas y revisión independiente | Cuatro fases, PR seriales y tres lentes | Cuatro fases; PR pequeños; revisión sobre el SHA final |
| Pruebas | Checks por etapa, sin frecuencia completa definida | Puntuales por PR, completas al cierre y CI | Puntuales locales + PostgreSQL pertinente antes de merge; CI requerido; completas locales al cerrar fase |
| Costo de producto | Política de selección pendiente | Última compra | Última compra confirmada por fecha de negocio, con procedencia y reglas de desempate; costo aplicado congelado por venta |
| Impuestos | Confirmar base/recuperabilidad | Régimen, tasa 21%, precio público bruto | Régimen guía tratamiento, pero tasa, base y composición se confirman por operación; 21% puede sugerirse, nunca convertirse en dato confirmado por defecto |
| Envío | Motor de compra existente | Sumar shipping_percentage al leer margen | Calcular al registrar compra, sobre la misma base y comprobante, sin sumar evidencia histórica otra vez |
| Promedio | Realizado ponderado y cobertura | Ponderado, vacío bajo 50% | FactsService canónico, cobertura monetaria y estado de calidad según regla real del proyecto; no equiparar cobertura con completeness |
| Identidad | Correspondencia conservadora | Código externo o contenido + ordinal | Identidad de operación/línea; datos originales; ambiguos se revisan, no se emparejan arbitrariamente |
| Socios | Reversión y compatibilidad detalladas | Modelo y cuatro pasos resumidos | Preservar contratos, PIN, reparación repetible y separación de flujo/caja |
| Alcance de UI | Productos, ventas, Otros y dashboard | Productos y dashboard | Mantener las cuatro superficies solicitadas |

## Contrato de ejecución

- Antes de cada paso se presenta el mini-plan con diff esperado, decisiones y aceptación; se espera el OK del dueño. Si autoriza expresamente toda una fase, ese OK cubre sus pasos y no se solicita de nuevo dentro del alcance. Por ahora solo se está preparando el plan.
- Cada paso parte de main actualizado y verifica qué ya se integró; los commits antiguos son referencias, no cherry-picks ciegos. Registrar PR/commit/resultado en una matriz de portado.
- Coordinar hasta cuatro agentes activos contando al coordinador. Implementación y frontend pueden trabajar en archivos distintos tras acordar contrato. Para revisión, liberar esos slots: coordinador + tres revisores de integridad, invariantes y pruebas.
- Consolidar hallazgos; verificadores adversariales trabajan después, en tandas dentro del límite. No descartar un riesgo material solo por no reproducirlo rápidamente. Corregir, volver a probar y revisar el diff final.
- El SHA completo revisado debe ser el mismo con checks verdes que se usa para merge. Verde no sustituye revisión. Si hay autorización de merge, usar la protección del head y sincronizar main después.
- Comprobar integración con main vigente; actualizar/rebasar cambia el SHA y exige checks pertinentes y revisión de los cambios nuevos antes de merge.
- Dos vueltas sin resolver un CI rojo disparan informe de bloqueo técnico y siguiente acción, no aceptación del fallo. Distinguir regresión, configuración e infraestructura; no repetir para obtener un verde casual.
- No ejecutar `ruff format`, `make format` ni `git add -A`. Documentación del dominio en el mismo commit. Usar fecha de negocio en pruebas. Attribution/trailers únicamente si corresponden al trabajo real y política del repositorio; no inventar autoría por copiar este workflow.
- Aplicar las guías de PostgreSQL antes de escribir esquema/políticas; migraciones aditivas al head real. Revisar seguridad, permisos y tenant para cada comando. Los contratos toleran frontend/backend desplegados en distinto orden.

## Fase 1 — cerrar pérdida de datos: B·3 y B·nuevo

### 1. B·3: categoría y códigos

- `build_incomplete_product`: rubro e inferencia existente, solo asignar categoría vacía con confianza alta. Media puede sugerir; baja no inventa. Respetar edición y `requires_completion`.
- Merge: si ganador no tiene código, transferir código y fuente del descartado. Si ambos tienen, mantener ganador y auditar el descartado/conflicto.
- Incluir normalización, snapshots, fingerprint y undo. Liberar la restricción única del descartado antes de transferir, dentro de la misma transacción. No cambiar stock ni precio por este paso.
- Pruebas: inferencia alta/ambigua, código presente/ausente/conflictivo, undo con modificación posterior, rollback y concurrencia PostgreSQL.

### 2. B·nuevo: preservar edición sin depender de posición

Diseñar y revisar la correspondencia antes de cambiar `_split_records` y `_reconcile`:

- Separar identificador lógico, datos originales importados y localizador hoja/fila. La edición manual no cambia los datos originales usados para reconciliar.
- `clave_de_operacion` puede identificar documento o línea. Documento multilínea requiere discriminador de línea. `external_code` de producto/cliente/proveedor no identifica una venta.
- Sin ID de origen: correspondencia uno a uno sobre evidencia original y contexto estable. Contenido + ordinal no garantiza identidad al insertar/eliminar duplicados. Ambigüedad obliga a revisión; no copiar edición a otra operación.
- Revisar también huellas de importación, exclusiones, pendientes reclasificados, stock, medición de pérdidas y F11. Mantener la compuerta transaccional vigente.
- Legacy: enriquecer metadatos solo donde se demuestre la correspondencia; los demás casos conservan su edición y se resuelven en revisión.
- Correspondencia ambigua bloquea apply y pide resolución explícita sin modificar datos; no conservar una edición creando además su duplicado ni omitir otra fila para sostener los conteos.
- Pruebas: ordenar/insertar/eliminar filas, gemelas, comprobante multilínea, mismo código con múltiples ventas, contexto totalmente editado, cambio de origen/parser, dos relecturas, clasificación desde Otros, stock y reversión. Sin aceptación de pérdida, rollback completo.

Cierre: suites completas backend/frontend aplicables y PostgreSQL real. Ensayo ASTERIA con filas desplazadas demuestra no duplicar edición ni saltar otra fila. No adelantar ingesta nueva de aportes sobre una identidad sin resolver.

## Fase 2 — costo y margen, capital en dinero

Los pasos son seriales al tocar importación o Facts. El diseño de capital y el de costos pueden investigarse en paralelo. La reparación de retiros es independiente del margen bruto, pero debe estar conciliada antes de presentar resultado neto actualizado.

### 3. M·0: diagnóstico y contrato monetario

- Diagnóstico SELECT-only del margen en cero de ASTERIA: precio/costo, cantidad/unidad, fuente, flags envío/impuestos, régimen y diferencias entre catálogo, Facts y dashboard. Sin imprimir URL ni secretos.
- Usar `purchase_cost.py`/`purchase_cost_decision.py`, no otra calculadora de compras. Dominio puro para margen, Decimal y redondeo explícito; FactsService para agregados.
- Registrar las tres decisiones relatadas en el plan aportado: régimen interviene en margen; última compra para costo de referencia; dashboard ponderado por ventas. No ampliar esas decisiones a tasas o inclusión de impuestos que no se declararon.
- Aprobar ejemplos para bases, selección de última compra, costo offline y cobertura antes de construir UI.

### 4. M·1: origen y costo comparable

- Última compra significa última compra confirmada válida por fecha de negocio, no última fila procesada. Definir desempate estable, comprobantes atrasados, anulaciones y correcciones. Revertir la compra elegida vuelve a seleccionar una fuente válida. Conservar edición manual y las protecciones de costos con/sin flete hasta decidir expresamente su reemplazo por una base comparable.
- Para venta importada/offline, fecha de corte = fecha de venta; una compra posterior no es elegible por sincronizar más tarde. Definir precedencia de costo manual confirmado y stock inicial/aportado cuando no hay compra, manteniendo su origen y sin presentarlos como compra inexistente.
- Contrato versionado: monto y cantidad/unidad, descuento, envío incluido/adicional/no aplicable/desconocido, impuestos incluidos/adicionales/no aplicables/desconocidos, tipo/tasa/importes, tratamiento confirmado, fuente/fecha/comprobante y política aplicada. Aceptar campos viejos sin marcarlos confirmados retroactivamente.
- `monto_incluye` describe ajustes ya contenidos; no prueba presencia de IVA. La decisión de IVA es independiente. `shipping_percentage` es evidencia informativa: solo puede usarse con base y procedencia confirmadas de la misma compra y decisión explícita; no se suma al consultar el producto.
- Envío compartido: repartir solo dentro del mismo comprobante, conservando la suma al centavo. Envío de línea y compartido siguen separados. Si ya integra costo, no deducirlo de resultado otra vez; caja conserva el pago real.
- Régimen fiscal guía la comparación, pero no determina por sí solo tasa ni recuperabilidad de cada comprobante. Precio público no prueba base fiscal de una venta importada. El alias legacy `registered` no habilita cálculos fiscales confirmados sin aclarar el régimen original.
- No aplicar 21% automáticamente ni multiplicar todo costo `sin_iva`: impuesto no aplicable es distinto de impuesto adicional. RI con tratamiento recuperable confirmado usa bases netas comparables; en otros tratamientos el impuesto no recuperable integra costo cuando efectivamente corresponde.
- Componentes de mercadería y envío pueden tener tasas/bases distintas: no dividir el total completo por una tasa única sin evidencia. Recuperable ya incluido se extrae por importe o base/tasa confirmados; no se resta dos veces.
- Si falta base comparable, mostrar motivo/estimación separada y excluirla del margen confirmado. Costo cero de producto incompleto es pendiente; solo un cero explícitamente confirmado podría ser un costo conocido si se habilita ese contrato.
- Las marcas en custom_fields requieren schema/validación de claves reservadas, versión y consumidores; «sin migración» no implica «sin contrato».

Referencia técnica fiscal: ARCA documenta distintas alícuotas y distingue crédito facturado/computable. Esto fundamenta no reducir el contrato a «régimen + 21%»; no se implementa un liquidador fiscal. [Diseño oficial IVA Simple](https://www.arca.gob.ar/iva/documentos/LID-Ajustes-y-otros-conceptos-para-generar-la-DJ.pdf), [especificaciones especiales](https://www.arca.gob.ar/iva/iva-simple/especificaciones-especiales.asp).

### 5. M·2: margen por producto y venta

- Catálogo: margen estimado con precio vigente y costo comparable de referencia de última compra. Construir contexto de régimen en servicio/assembler antes del schema; la propiedad de Pydantic no hace consultas ni deduce tenant.
- Venta: congelar costo y bases por línea al confirmar. Última compra al momento de vender no significa revaluar ventas pasadas con la compra de hoy. Aprovechar movimientos históricos cuando sean fuente válida, incluyendo ventas sin efecto de stock.
- Offline: cachear versión y procedencia del costo; conservar snapshot enviado y validarlo como dato de cliente. No confiar en un costo arbitrario enviado por el navegador. Si falta evidencia, la venta puede sincronizar con margen pendiente, sin inventar costo. Idempotencia conserva el mismo snapshot al reintentar una respuesta perdida.
- Para cajeros sin acceso al costo, usar referencia opaca validable por servidor o versiones históricas reconstruibles; no entregar el importe protegido al navegador. El snapshot es de la política de última compra/referencia aprobada: se etiqueta esa metodología, no se promete costo real por lote.
- Import antiguo/alias: reconstrucción histórica solo con evidencia, auditada. Costo vigente puede ofrecer estimación explícita, nunca etiquetarse como realizado histórico.
- `ganancia bruta = ingreso comparable tras descuento − costo vendido`; `margen % = ganancia / ingreso × 100` si ingreso > 0. Diferenciar de recargo sobre costo. Margen negativo válido; sin costo/base comparable, resultado pendiente.
- No agregar impuestos a lo ya incluido. Para productos por peso, cantidad normalizada a unidad de venta.
- API, Facts, finanzas determinísticas y chat comparten definición; corregir el cálculo denominado bruto que hoy resta todos los gastos.

### 6. M·3: dashboard, productos, ventas y Otros

- Dashboard: fact canónico «Margen bruto de ventas» del período, tarjeta, gráfico histórico y desglose por categoría; endpoint schema Pydantic, filtros comunes y sin fórmula independiente en React.
- Promedio: `100 × Σ(ingreso comparable − costo) / Σ ingreso comparable`, solo ventas con costo/base confirmados. Mostrar ganancia conocida, cobertura por importe vendido y monto/ventas excluidos. «X de N productos» es información secundaria, no el peso del promedio.
- Cobertura usa bruto vendido elegible / bruto vendido total sobre el mismo universo; margen usa bases comparables normalizadas. No dividir neto confirmado por bruto total. Mostrar la metodología de costo de última compra/referencia junto al indicador.
- Diferenciar cobertura de costo y completeness general. Respetar el bloqueo existente de analítica con calidad LOW. Si se aprueba además mínimo 50% de cobertura, codificarlo como política separada con límites 49,99/50/50,01; no atribuirlo automáticamente a la regla existente. Resultado parcial elegible siempre dice su cobertura.
- Productos: columna margen estimado y desglose con motivos. Ventas: margen aplicado por línea/operación, según permisos. Otros de compra: resolver producto/base/cantidad/envío/impuestos mediante el mismo comando. Otros de venta: costo/margen pendiente y resultado al resolver; el alias no mueve stock.
- No publicar margen en tickets de clientes ni en contratos/cache accesibles al cajero sin permiso. Revisar permisos también en backend, no solo ocultar columnas.
- No duplicar una venta mixta por los pagos. Excluir anuladas/demo/capital según filtro; separar bruto, neto y flujo.

Aceptación M: ventas $100/costo $80 y $900/costo $450 → ganancia $470, margen 47%, no promedio simple 35%. Compra de 10 unidades: mercadería $1.000 + envío $100 + impuesto no recuperable confirmado $210 → $131 por unidad; si ya incluidos, no se agregan. Caso RI: venta bruta $1.210 y costo bruto $605, ambos con IVA 21% y tratamiento recuperable confirmado → bases $1.000/$500 y margen 50%. Probar además no aplicable, tasas distintas por componente, costo/base desconocidos, compra posterior, offline, anulación y relectura.

Prueba offline crítica: venta anterior a una nueva compra, sincronizada después, conserva la fuente elegible de su fecha; respuesta perdida y reintento crean una sola venta y un solo snapshot, sin filtrar costos al cajero.

### 7–10. D·2–5: capital en dinero

7. Modelo `owner_capital_movements`, comandos auditados y carga manual: aporte/retiro/préstamo socio/devolución, dinero/especie, socio opcional, fecha de negocio, medio de pago, importe positivo, procedencia y anulación. PIN y tenant. Cambiar escrituras nuevas antes de reparar históricos; conservar endpoint y respuesta compatible de `/expenses/profit-withdrawal` para frontend anterior.
8. Reparación: dry-run por tenant/período, únicamente `profit_withdrawal`, vínculo al gasto original, reejecución sin duplicados, reversión y conciliación de gastos/margen/score. Fuera de Alembic. Diagnósticos son SELECT-only; este `--apply` es una escritura explícitamente autorizada y separada.
9. Facts/Balance: aportes y retiros por socio, dinero/especie, préstamos pendientes; flujo operativo separado del flujo con capital y del arqueo contado. No llamar balance contable a esta vista analítica.
10. Onboarding: capital inicial opcional declarado (`PESO_DECLARADO`), fechado, sin duplicar al repetir ni confundir con saldo de caja actual/aporte previo.

Aceptación D: aporte $100.000 y retiro $10.000 → flujo capital $90.000, sin nuevas ventas ni gastos operativos ni cambio de margen bruto. Reparar retiro antiguo sí elimina su efecto incorrecto previo sobre resultado/score, una sola vez y con explicación. Conservar compatibilidad de respuesta y permisos/PIN.

Cierre fase 2: suites completas aplicables, PostgreSQL, ensayo conjunto M+D y conciliación entre detalle, tabla, serie y tarjeta. El diagnóstico pendiente de producción se informa; no se inventa causa del margen cero ni se da por hecho un smoke no realizado.

## Fase 3 — B·4–9: calidad, resolución y alias

11. B·4: F-N revisado, completa solo vacíos y respeta `has_user_edits`. Totales solo con etiqueta exacta y evidencia conservadora de agregación; «Total Clean» permanece. Una etiqueta Total no basta para borrar una operación descriptiva válida.
12. B·5: resumen Otros por archivo/motivo, conteos del grupo completo y descarte con preview/confirmación, no solo página visible. Contrato de resolución M reutilizado.
13. B·6: campos cruzados solo vacíos y una vez por entidad; reutilizar `UPDATE_*` si expresa la reversión completa. Ledger/F11 protege ediciones posteriores. Verificar capacidad antes de prometer ninguna migración.
14. B·7: cliente/proveedor por código normalizado y único del tenant. Código desconocido/conflictivo no crea maestro ni elige uno arbitrario.
15. B·8a: alias con tabla propia/procedencia, después de barcode/SKU y fuera del escaneo POS. Ambigüedad no vincula. Vincular actualiza producto/margen de modo auditado sin stock implícito. Merge mueve aliases; resolver choque no sobrescribe otro producto. F11 no borra aprendizaje manual.
16. B·8b: pantalla sobre contrato aprobado, resolución individual, conflicto, costo/base pendientes y efecto confirmado. Evitar alias falso positivo o asignación masiva silenciosa.
17. B·9: solo «Guardar como campo propio» e «Ignorar vacías». No «Aceptar ambiguas» ni bypass de user_selected/riesgo.

Cierre: suites completas, PostgreSQL pertinente y smoke de import con totales, múltiples hojas, Otros, campos cruzados, código y alias. Comparar datos antes/después y undo; una venta resuelta concilia con M.

## Fase 4 — D·6–7 y cierre

18. D·6: mercadería aportada = stock `opening_balance` + aporte valorizado declarado, atómicos. Sin gasto ni salida de dinero al aportar; costo vendido entra en M/COGS. Equipamiento informativo, sin amortización. Definir reversión si stock ya se vendió, no borrar vínculo o provocar stock inválido.
19. D·7: destinos libro mixto, Diario y hojas sueltas; identidad estable, revisión, procedencia, ledger y F11. Préstamo bancario o sin socio identificable sigue en Otros. Reimportar/releer no duplica capital/stock ni pisa edición.

Cierre fase 4: suites completas y PostgreSQL; ensayo de aportar mercadería, vender parte y anular/releer bajo política aprobada. Al finalizar, agente de completitud contrasta alcance contra matriz, pruebas y pendientes reales.

20. Cerrar #48: listar portado/descartado/PR y evidencia; distinguir capital/margen nuevo del portado original. Solo cerrar y borrar su rama con autorización aplicable, integración verificada y sin commits útiles pendientes. No borrar `spike/pos-b1`; hardware y `.venv312` siguen fuera.

## Verificación por PR y por fase

- Local por PR: pruebas dirigidas y dependencias afectadas; mutación específica de protección crítica cuando aporte evidencia; ruff check, mypy exigido por repo, frontend type-check/lint y Jest pertinente, diff check/stat. PostgreSQL antes de merge cuando toca identidad, unicidad, RLS, locks, migraciones o reversión.
- No repetir toda la suite local en cada paso. CI actual backend ya ejecuta suite/cobertura y PostgreSQL; frontend ejecuta Jest/type-check/build. Exigir los jobs aplicables completos, no un verde de job ajeno o salteado por paths.
- Cierre de cada una de las cuatro fases: suites completas locales con entorno equivalente al CI y PostgreSQL aislado; registrar SHA, comandos, cantidades y skips. Confirmar que tests postgres se ejecutaron, no que quedaron omitidos por DSN faltante.
- Fallo de cierre bloquea la fase siguiente; arreglo en PR revisado. Checks de identidad/margen/capital críticos no se posponen hasta ese cierre.
- Smoke local/demo y fixtures ASTERIA son trabajo ejecutable por agentes. Producción depende de acceso y autorización disponibles; dar al dueño solo la acción que realmente no podamos ejecutar, con script/consulta concreto sin secretos. Nunca afirmar corroboración de producción sin evidencia.

## Primer paso a aprobar

B·3 solamente: inferencia alta en categoría vacía, transferencia de código/fuente respetando unicidad, auditoría/snapshots/undo y pruebas PostgreSQL. Antes de implementarlo, confirmar estado de main y presentar su mini-plan. El resto conserva sus dependencias y decisiones explícitas; este documento no autoriza su ejecución.
