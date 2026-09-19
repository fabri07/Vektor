# Suscripciones: cierre del backend, experiencia de usuario y cobro

Fecha: 2026-09-18. Estado: **bloques A y B implementados y probados (sin desplegar); C–G propuestos**.

Base: `planes-suscripcion-y-cupos.md` y revisión del código local. La suite de
5085 pruebas aprobadas y las pruebas de concurrencia son resultados reportados
por la implementación anterior; no se volvieron a ejecutar para redactar este
plan. El documento anterior indica que esa implementación aún no se desplegó.

**Objetivo y alcance**

Completar el control de suscripción en todos los recorridos de negocio, hacer
recuperables los consumos interrumpidos, permitir operar un piloto por
transferencia y después incorporar la pantalla de suscripción y Mercado Pago.
Reutilizar catálogo, estados, contadores, reservas, auditoría y scripts existentes.

Conservar las decisiones acordadas: aprobación humana; prueba de 14 días desde
la aprobación; plan asignado explícitamente; cupos compartidos por empresa;
gracia de siete días con el saldo anterior; FREE legado preservado; correcciones,
consulta, seguridad y exportación accesibles. La corrección no concede lecturas
de IA o importaciones nuevas gratuitas: siempre queda disponible el camino manual
autorizado para corregir registros existentes.

**Orden de entregas**

| Bloque | Trabajo | Depende de | Condición de salida |
|---|---|---|---|
| A | Corregir bordes del acceso y reservas existentes | Base actual | Vencimiento/cancelación y reintentos sin bypass |
| B | Completar escrituras y usuarios | A | Misma política por API, chat y workers |
| C | Importaciones, extracciones y recuperación | A; coordinación con B | Consumo único, sin reservas abandonadas sin tratamiento |
| D | Renovación manual y operación del piloto | A–C | Ciclo pago, vencimiento y renovación verificable |
| E | API de consulta y frontend | Contratos de A–D | Usuario ve condiciones, consumos y restricciones |
| F | Mercado Pago | Servicio comercial de D; E para flujo público | Cobro recurrente y conciliación completos |
| G | Despliegue gradual y verificación integral | D/E para piloto; F para débito automático | Evidencia operativa y procedimiento de recuperación |

**A. Cerrar inconsistencias antes de extender el mecanismo** — CERRADO

Hecho: puntos 1, 2, 3 y 5 (como primitivas `autocommit=False`), más un
agujero que el plan no listaba: `expire-due` pasaba a `GRACE` los FREE legado
con período viejo. Detalle en `planes-suscripcion-y-cupos.md` → "Bloque A".
**Precondición de despliegue:** `scripts/subscriptions.py diagnose` contra
producción. Diferidos: el punto 4 (clave estable del chat) pasa al bloque E
porque exige cambio del cliente; el punto 6 (éxito unificado chat/SSE y
resultado durable) queda con el bloque C, que introduce el resultado durable.

Archivos principales: `app/domain/subscription.py`,
`app/application/services/subscription_service.py`,
`app/persistence/repositories/tenant_repository.py`, `app/api/v1/deps.py` y
`app/api/v1/agent.py`.

1. Evaluar un ACTIVE pago por fecha: antes del fin está activo; desde el fin
   hasta fin + siete días tiene acceso de gracia; desde el fin de gracia queda
   restringido aunque el estado persistido todavía diga ACTIVE. Usar intervalos
   `[inicio, fin)` y tratar FREE legado explícitamente. Hoy la rama ACTIVE de
   `effective_access()` devuelve acceso sin evaluar su vencimiento.
2. Unificar cancelación: usar `cancel_at_period_end` mientras queda servicio pago
   y pasar a CANCELLED al finalizar; contemplar datos existentes ya CANCELLED.
   Resolver también la última suscripción cancelada para informar su situación.
   Hoy el repositorio la excluye y tanto el gate como el helper de chat permiten
   continuar si reciben `None`: no confundir ausencia con FREE. Una cancelación
   vencida debe bloquear nuevas operaciones; una inconsistencia sin suscripción
   debe dar error temporal estructurado y alerta, conservando consulta/exportación.
3. Hacer que `reserve()` devuelva un resultado explícito: reserva nueva,
   ejecución en curso, resultado ya confirmado o intento liberado. Hoy todos los
   conflictos son un retorno silencioso; eso no impide que un caller vuelva a
   ejecutar IA con una reserva RELEASED o COMMITTED. Resolver cada caso antes
   de ejecutar, validando unidades y contenido de la solicitud.
4. Separar identidad de operación lógica de identidad de intento/lease.
   Los UUID nuevos por petición de chat no deduplican reintentos HTTP. Incorporar
   una clave estable, acotada al tenant y vinculada al contenido; una misma clave
   con contenido distinto devuelve conflicto. Mantener compatibilidad transitoria
   con clientes antiguos, explicitando que sin clave estable no hay deduplicación
   entre peticiones. Una reserva RESERVED tampoco autoriza dos ejecutores.
5. Revisar los commits internos de `reserve/commit/release`: no deben publicar
   cambios de negocio pendientes en una sesión compartida. Proporcionar primitivas
   sin commit para composición transaccional y una envoltura con transacción corta
   para IA. Hacer rollback antes de compensar un error que invalide la sesión.
6. Unificar qué se considera éxito entre chat normal y SSE. Registrar resultado
   durable e identidad de reserva antes de confirmar consumo. Una desconexión no
   devuelve automáticamente cupo si el resultado quedó completado y recuperable.
   Cubrir también cancelaciones asíncronas y fallos antes de iniciar el generador.

Criterios: ACTIVE vencido sin cron, límites temporales exactos, CANCELLED con y
sin período restante, tenant sin suscripción, operación repetida en los tres
estados de reserva y fallo de la sesión de DB. No reabrir acceso al faltar datos.

**B. Completar el control de operaciones y usuarios** — CERRADO

Hecho todo lo de abajo salvo el rechazo de downgrade con plazas excedidas,
que no tiene dónde vivir hasta que exista el cambio de plan (bloque D).
«Otros» se bloquea (decisión de producto). Detalle en
`planes-suscripcion-y-cupos.md` → "Bloque B".

Inventariar operación, endpoint, servicio ejecutor, worker y clasificación:
creación, corrección, consulta, seguridad o cancelación. Clasificar por efecto,
no por verbo HTTP: un PATCH puede activar una automatización y un POST corregir
un dato existente. Las correcciones conservan roles, PIN y auditoría actuales.

Conectar el gate en nuevas operaciones de productos/stock, cierres de caja,
clientes/proveedores, remitos, campos personalizados y automatizaciones. Incluir
altas masivas, confirmaciones de acciones del agente, confirmaciones mediante
texto y reintentos. El router de confirmación determinística del chat se ejecuta
antes de reservar IA: necesita control de la acción aunque no consuma una consulta.

Aplicar el servicio de autorización también en ejecutores internos; una
dependency de FastAPI no se ejecuta automáticamente cuando un worker llama a una
función Python. Evitar repetir el gate en subpasos de una misma operación ya
autorizada. Corregir una venta puede requerir movimientos compensatorios de stock:
deben conservarse bajo el contexto de esa corrección, sin habilitar altas nuevas.

Para usuarios, ajustar `enforce_seat_limit` para adquirir el lock **antes de
obtener el conteo**, y conservarlo hasta insertar/reactivar y confirmar. Hoy
recibe un entero ya calculado, por lo que su firma no asegura ese orden.
Obtener el límite del acceso efectivo, incluyendo un solo usuario durante trial.
Cubrir cualquier alta/reactivación disponible, no solo POST /users. El dueño
ocupa una plaza; usuarios inactivos no. Las bajas siguen disponibles. Rechazar un
downgrade si excede plazas, explicando cuántas deben desactivarse; no borrar usuarios.

Criterios: cuenta vencida bloqueada por API, agente y worker; correcciones y
exportación funcionan; dos altas concurrentes disputando la última plaza producen
exactamente un alta. FREE existente no pierde usuarios al desplegar.

**C. Conectar consumos y reconciliar reservas**

**C1. Importaciones.** El punto real `confirm_file` está en
`app/api/v1/ingestion.py`; el ejecutor asíncrono lo llama desde
`app/jobs/import_executor_worker.py`. Reutilizar la identidad del intento y la
huella de petición existentes. Cubrir tanto confirmación síncrona como registro
asíncrono, redelivery y recuperación de workers.

En el recorrido asíncrono, autorizar y reservar al registrar el trabajo en la
misma transacción que el intento y su orden de ejecución. El worker reutiliza la
reserva: no cobra otra unidad. La reserva válida permite terminar después del
vencimiento comercial; una ejecución nueva sin reserva exige autorización nueva.

En el recorrido síncrono, coordinar lease y reserva en una transacción corta,
adaptando los helpers con commit interno. No dejar un archivo IMPORTING si falla
la reserva. Conservar la protección por token frente a takeover. La confirmación
de consumo se persiste junto con los datos importados y el resultado terminal;
si esto exige separar transacciones, registrar evidencia durable para que la
reconciliación cierre la diferencia, sin volver a importar datos.

No consumir por validación rechazada antes de ejecutar, doble confirmación o
reintento técnico. Una importación con resultado útil persistido y filas
rechazadas identificadas consume una unidad; un rollback total por fallo técnico
libera. El consumo se vincula al archivo/revisión lógica autorizada, no a cada
lease de ejecución.

**C2. Foto/PDF.** Conectar reserva en los recorridos que llaman a
`remito_extraction_service.py` y `customer_extraction_service.py`, pasando contexto
de tenant y operación desde sus callers. Validar formato, tamaño y páginas antes
de reservar o llamar al proveedor. Una lectura es una extracción exitosa de un
documento dentro de los máximos publicados; sus reintentos internos no son nuevas
unidades. Registrar el resultado para poder reutilizarlo tras un timeout.

Leer un documento y luego importar sus datos son dos recursos distintos cuando
ambas operaciones se solicitan; evitar consumir importación al crear manualmente
una entidad a partir de una lectura si no recorrió el importador.

Auditar los límites de archivo ya existentes y completar máximos explícitos de
páginas, MB, filas y hojas antes del piloto. Presupuestar también interpretación
previa a confirmar, relecturas y ayuda con IA. Los errores cuestan tokens aunque
no consuman unidades del cliente. La ayuda no debe convertirse en una vía de IA
sin límite ni telemetría; conservar disponibles las FAQs estáticas.

**C3. Reconciliación.** Vincular reservas con intento, lease/token de ejecución,
resultado durable y tiempos de actividad. Añadir solo los campos/índices que no
puedan resolverse con las estructuras actuales; cualquier migración respeta el
aislamiento por tenant.

Agregar `reconcile-reservations` con dry-run y un job periódico. Procesar lotes
acotados con exclusión concurrente y transacciones cortas. Para cada reserva:

- Resultado exitoso persistido: confirmar una sola vez.
- Fallo definitivo o ejecución cancelada sin resultado útil: liberar una sola vez.
- Ejecutor con lease válido: no tocar.
- Ejecución abandonada demostrada: impedir que un worker viejo publique resultados
  mediante token/versionado y después liberar o retomar con el protocolo existente.
- Evidencia ambigua: conservar, alertar y permitir resolución auditada.

No liberar únicamente por antigüedad. La acción manual identifica reserva, motivo
y operador. Observar cantidad/edad de pendientes y discrepancias entre contadores
y reservas. Programar también `expire-due`; la autorización permanece independiente
del scheduler.

Criterios C: última unidad bajo concurrencia Postgres; redelivery; takeover; caída
antes/después del commit; cuota agotada sin lease colgado; broker caído; SSE
desconectado; job de reconciliación concurrente con finalización; aislamiento de
dos empresas. Una operación no debe dejar datos confirmados con consumo perdido
ni cobrar dos veces por la misma importación.

**D. Renovación manual y piloto por transferencia**

Extraer un servicio comercial común a scripts y futuro gateway: activar,
registrar renovación, programar cambio de plan, cancelar renovación y reactivar.
Mantener set-status como reparación operativa validada y auditada, sin que conceda
períodos/cupos inconsistentes o reinicie pruebas accidentalmente.

La activación actual busca la referencia en auditoría antes de escribir. Añadir
unicidad en DB para pagos/comandos comerciales y lock transaccional por suscripción:
dos ejecuciones concurrentes de la misma referencia no conceden dos períodos.
Rechazar la misma referencia con datos diferentes. Registrar fuente, referencia,
tenant, importe, moneda, operador, período y copia de condiciones; conservar la
auditoría además del registro comercial.

Separar primera activación de renovación: hoy activate vuelve a empezar el período
desde ahora. Un pago anticipado debe otorgar el período siguiente sin borrar el
saldo actual. Propuesta para el piloto: renovación durante gracia mantiene el
ancla anterior; reactivación después de gracia comienza al confirmar el pago.
Mostrar siempre las fechas en el dry-run antes de aplicar.

Definir mensualidad por aniversario calendario, incluida la regla de fin de mes,
para armonizarla con el gateway; hoy el script usa 30 * months días. Preservar
fechas ya otorgadas. Limitar el piloto a un mes por período: --months > 1 requiere
ciclos de cupo mensuales separados y no debe simularlos con un único cupo extendido.
Los cambios de plan del piloto se aplican al siguiente período; no ofrecer
prorrateo inmediato antes de implementarlo.

Criterios: pago repetido simultáneo, pago adelantado, renovación en gracia,
reactivación, cambio de plan sin reset anticipado y cancelación que conserva el
período pago. Downgrade con usuarios excedentes y reinicio de trial rechazados.

**E. API de suscripción y frontend**

Crear un contrato de consulta autenticado, por ejemplo GET /subscription, que
devuelva plan, estado persistido y efectivo, condiciones otorgadas, fechas,
cancelación programada, cuotas usadas/reservadas/disponibles y plazas ocupadas.
La consulta sigue disponible con suscripción vencida/cancelada. No exponer datos
de otro tenant ni referencias internas de pago a roles sin autorización.

Los miembros pueden ver disponibilidad relevante para trabajar; el OWNER puede
ver y gestionar cobro/cancelación. La política de roles se valida en backend.
Separar vencimiento del período de renovación efectivamente confirmada: con
transferencia manual, alcanzar una fecha no garantiza cupo nuevo.

Implementar pantalla de plan y consumo, período de prueba, pago por transferencia,
historial de pagos y cancelación. Avisos al aproximarse el vencimiento o al alcanzar
80%/100% de cada recurso; mensaje específico al recibir 402/429 o límite de plazas.
Explicar que las correcciones siguen disponibles: “solo lectura” es un nombre
interno que no describe por completo esa excepción comercial.

No esconder datos ni cerrar sesión al vencer. Conservar borradores ante un rechazo.
Reconsultar estado al recuperar foco, después de pagos/cambios y ante errores de
suscripción. Adaptar el cliente SSE para leer los errores HTTP anteriores al stream.
No presentar un registro de pago como factura fiscal; definir por separado cómo
se generan o adjuntan los comprobantes del servicio.

Mercado Pago se incorporará luego como modalidad de contratación sobre esta misma
pantalla. No requiere construir un panel administrativo nuevo.

Criterios: trial → activa → gracia → restringida, todos los estados visibles;
cuenta cancelada puede ver pagos/exportar; OWNER gestiona y los demás roles no;
formularios conservan trabajo; móvil/escritorio; errores de chat normal y SSE.

**F. Mercado Pago: Etapa 3**

Integrar un adaptador de suscripciones sin duplicar la lógica comercial de D.
Persistir identificadores del proveedor y relación con tenant/suscripción;
crear checkout únicamente para un OWNER de una cuenta ya aprobada. Precio,
moneda y plan salen del servidor. Unificar mensualidad y fechas antes de habilitar.

Separar autorización de suscripción y acreditación de cada cuota. El retorno del
checkout y una suscripción autorizada no acreditan por sí solos un pago. Mercado
Pago expone eventos de suscripción y de cobros recurrentes por separado; consultar
el recurso oficial para decidir su resultado, incluidos rechazos/reintentos.

Implementar recepción validada de notificaciones según el mecanismo oficial de
cada tópico, almacenamiento durable, deduplicación, procesamiento reintentable y
conciliación periódica con la API. Confirmar los tópicos y soporte de firma en
pruebas del producto Suscripciones; no asumir que todos los avisos tienen el
mismo mecanismo de autenticación. Verificar proveedor/cuenta, tenant, monto,
moneda, referencia y resultado del pago antes de conceder derechos.

Reutilizar el registro único de pago: eventos repetidos o fuera de orden no
extienden períodos ni reinician cupos. Atender cobros rechazados, recuperaciones,
cancelación desde ambos lados, devoluciones y contracargos con reglas explícitas
de acceso/auditoría. Reintentos del proveedor no conceden nuevas gracias; evitar
cobranza simultánea manual y automática para el mismo período.

La actualización de precios afecta futuros cobros mediante versión/fecha efectiva
y comunicación previa, sin reescribir períodos pagados. Sin anuales, descuentos,
paquetes adicionales ni prorrateos en la primera integración.

Criterios: pruebas de integración de aprobación, pendiente y rechazo; duplicados,
fuera de orden, firma inválida cuando corresponda, recurso de otra cuenta,
notificación perdida recuperada por conciliación, API caída y doble click en
checkout. Renovación manual y automática producen los mismos derechos.

Referencias oficiales consultadas para el alcance de esta fase:

- [Resumen de Suscripciones](https://www.mercadopago.com.ar/developers/es/docs/subscriptions/overview).
- [Pagos autorizados y reintentos](https://www.mercadopago.com.ar/developers/es/docs/subscriptions/integration-configuration/subscription-no-associated-plan/authorized-payments).
- [Notificaciones y tópicos](https://www.mercadopago.com.ar/developers/es/docs/checkout-bricks/additional-content/your-integrations/notifications/webhooks).
- [Actualización de suscripciones](https://www.mercadopago.com.ar/developers/es/reference/online-payments/subscriptions/update-preapproval/put).

**G. Verificación y despliegue**

Ejecutar primero pruebas focalizadas de cada bloque, concurrencia en Postgres,
lint/tipos y luego la suite completa al integrar. Las pruebas de migración y
aislamiento se ejecutan sobre los cambios nuevos; los resultados previos no
sustituyen los escenarios añadidos en A–F. Probar un ciclo completo con reloj
controlado, incluyendo fin de mes y finalización posterior al vencimiento.

Preparar migraciones aditivas, respaldo y diagnóstico de estados/referencias antes
de desplegar. No volver a correr seeds que cambien condiciones otorgadas. Documentar
rollback de aplicación y recuperación operativa: deshabilitar nuevos checkouts
no debe detener procesamiento de pagos recibidos ni recuperación de trabajos.

Piloto pequeño con activación manual tras A–E; observar costo real por recurso,
rechazos por estado/cupo, reservas pendientes, operaciones denegadas incorrectamente
y discrepancias de pago. Luego habilitar Mercado Pago gradualmente. FREE legado
continúa con su política actual; migrarlo a pago requiere un plan comercial aparte.

Actualizar `planes-suscripcion-y-cupos.md` y el resumen de CLAUDE.md conforme se
cierren bloques, distinguiendo implementado, probado y desplegado. La entrega
está completa cuando los tres recorridos —cliente, operador y proveedor de pago—
coinciden en período, permisos, consumo y estado, con recuperación demostrada.
