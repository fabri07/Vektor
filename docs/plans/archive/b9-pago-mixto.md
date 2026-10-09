# B9 — Pago mixto en la caja

## Objetivo y frontera

`POST /pos/operations` ya recibe una lista de `tenders`, pero rechaza cualquier
lista de más de uno. Si se quitara ese rechazo sin cambiar los lectores, cada
`SaleEntry` tomaría el método del primer pago: una venta de $1.000 con $600 en
efectivo y $400 fiados aparecería como $1.000 en efectivo y $0 de deuda.

B9 habilita varios pagos por ticket **después** de que cada lector atribuya cada
importe una sola vez. Un `tender` registra la imputación declarada por el cajero;
no procesa ni verifica un cobro con tarjeta, QR o transferencia.

**Precondición de activación:** cerrar las dos regresiones pendientes de B7:
recuperar una venta B13 cuyo primer POST hizo commit y perdió la respuesta sin
cambiar su huella, y no borrar automáticamente una operación marcada como cobrada
ante un 400/422. B9 aumenta las combinaciones de rechazo; activarlo antes haría
más frecuente perder la constancia local de un cobro. Los arreglos tienen pruebas
de regresión independientes y deben estar desplegados antes de quitar el guard.

## Contrato contable

- Una operación es mixta si tiene **más de un método distinto**, no simplemente
  más de un tender. Dos pagos `cash` siguen siendo una venta de efectivo.
- Las `SaleEntry` de una operación mixta llevan el marcador interno `mixed`.
  Las de un solo método conservan ese método. `mixed` no entra en
  `PaymentMethod`, `PAYMENT_METHOD_PATTERN`, las opciones de formularios ni los
  `PosTender`: ningún cliente puede declararlo como pago.
- Los importes por método se leen de dos poblaciones disjuntas: (A) `SaleEntry`
  vivas con método distinto de `mixed`; (B) **una fila por `PosTender`** de una
  `PosOperation` mixta `COMPLETED`. Se usan `operation_date` y `customer_id` de
  la cabecera. No unir directamente tender con líneas: tres líneas y dos pagos
  producirían seis filas y triplicarían los importes.
- La operación se escribe en una transacción: `sum(line_total_ars) ==
  pos_operations.total_ars == sum(pos_tenders.amount_ars)`, al centavo. El total
  de ventas y el costo continúan saliendo de `SaleEntry`; los tenders sólo
  distribuyen medios, liquidez y deuda. `cash_received_ars` y `cash_change_ars`
  no son ventas ni se suman a los tenders.
- `VOIDED` elimina las dos poblaciones de los agregados, sin borrar tenders ni
  el ticket auditable. Las filas de una operación siguen protegidas contra
  PATCH/DELETE individual por `BELONGS_TO_POS_OPERATION`.
- Todos los joins incluyen `tenant_id` y el scope compartido filtra estado,
  fecha de negocio y pertenencia **antes** de agregar. Para métricas con
  `include_demo`, la proyección de tender debe heredar la procedencia de las
  líneas de la operación mediante una subconsulta **de una fila por operación**;
  nunca se suma un tender REAL a ventas DEMO excluidas, ni viceversa. El writer
  POS actual crea líneas REAL; comprobar ese invariante y detectar una operación
  con procedencias mezcladas, sin asignarle una procedencia inventada.

`sales_entries.payment_method` es `String(30)` sin CHECK de valores y la tabla
`pos_tenders` ya existe: esta versión no requiere migración. Antes de desplegar
el lector, verificar contra el esquema migrado y con una consulta de sólo lectura
que no haya `SaleEntry.payment_method = 'mixed'` preexistentes o incompletas.

## Entrega 1 — lectores, con el rechazo de escritura todavía activo

Crear un scope de consulta compartido en
`persistence/repositories/_pos_tender_scope.py`. Debe exponer el predicado
`venta_no_mixta` y una proyección de tenders mixtos con `operation_id`,
`sale_date`, `customer_id`, `payment_method`, `amount_ars` y `provenance`, con
límites de fecha opcionales. La selección de operaciones mixtas se define una
vez a partir de sus métodos distintos. Ningún lector replica el filtro ni une
directamente `pos_tenders` a `pos_operation_lines`.

1. `SaleRepository.cash_breakdown_by_method`: agregar los importes por día y
   método de A y B; `mixed` nunca aparece como serie. Conservar la semántica
   actual de los cobros `inflow` y de las ventas históricas. El arqueo diario y
   `/insights/cash-breakdown` consumen esta consulta.
2. Deuda de clientes: en `get_receivables_by_customer`, `get_balance_by_customer`
   y `get_balances_by_customer`, sumar sólo los tenders `account` de B al fiado
   de A. Los cobros `inflow` se restan **una sola vez**. La consulta de saldos
   masivos debe partir de la unión de IDs de clientes con filas A, cobros o
   tenders B: un cliente cuya única deuda sea mixta no puede desaparecer del
   resultado. Cliente y tenant se toman de la operación, nunca de una línea
   repetida por producto.
3. Precisar el contador `n_sales`: hoy cuenta filas `SaleEntry` y, en el saldo
   masivo, incluso filas `inflow`; no representa tickets. Para B9, conservar
   ese contador legado sin cambiar resultados históricos, agregar una unidad
   por **tender `account`** mixto y cambiar el texto del agente que dice
   «N ventas» por «N movimientos». Contar tickets POS únicos, incluidos los
   monomedio de varias líneas, exige un cambio de semántica separado; no
   presentar este número como cantidad de tickets.
4. `business_state_service`: al flujo líquido de A añadir sólo tenders B de
   `_LIQUID_METHODS`; aplicar exactamente la misma ventana de fecha que a
   `SaleEntry`. Ni fiado ni crédito diferido entran en ese importe.
5. `FactsDataProvider` y `FactsFrames`: añadir
   `mixed_tenders(tenant_id, start, end)` con columnas `sale_date`, `amount_ars`,
   `payment_method`, `provenance`. `load_facts_frames` lo precarga para la
   ventana completa actual **y anterior**, `PreloadedFactsProvider` comprueba
   tenant/cobertura y lo recorta por período, y los proveedores falsos de tests
   devuelven el frame vacío con esas columnas. `_split_liquidez` recibe las
   ventas y el frame B filtrados por el mismo período/procedencia:
   `V = ventas reales`, `I = cobros inflow`, `F = account(A)+account(B)`,
   `D = crédito diferido(A)+crédito diferido(B)` y `L = V-F-D+I`.
   `ventas_periodo`, costo y márgenes conservan sus líneas originales.
6. Comprobar que `normalize_payment_method('mixed')` sigue resultando en
   `other` y que `save_sale` lo rechaza. La reparación de importaciones que
   busca `cash` con `product_id NULL` no procesa ventas POS, y no necesita
   incorporar el marcador.

Esta entrega se puede desplegar sola: con el guard activo, B está vacía. La
prueba de despliegue incluye la consulta de datos previos para confirmar esa
premisa, no sólo que el CI esté verde.

## Entrega 2 — habilitar la escritura en el servidor

1. Quitar `MIXED_TENDER_NOT_ENABLED` **después** de Entrega 1. Derivar el método
   de las líneas del conjunto de métodos de `body.tenders`; si hay uno, usarlo;
   si hay varios, usar la constante interna `MIXED_PAYMENT_METHOD`.
2. Resolver el cliente como fiado si **cualquier** tender es `account`. Exigir
   permiso `fiado` y cliente real del mismo tenant, nunca el centinela «Local».
   Mantener la comprobación del tope de crédito sobre ese cliente y probar que
   la deuda nueva incrementa sólo la parte `account`.
3. Mantener `validar_tenders`, los importes positivos y el vuelto contra la
   **suma de los tenders cash**, no contra el total. `cash_received_ars` sin
   tender `cash` sigue dando 422. Limitar a 10 tenders en el request; métodos
   repetidos son válidos por API aunque la UI prefiera una fila por método.
   `mixed` se rechaza como método solicitado.
4. No introducir un nuevo campo ni cambiar la huella de un pedido congelado.
   El guard 422 está después del claim de idempotencia, pero la dependencia de
   sesión hace rollback ante excepción; una prueba HTTP debe confirmar que el
   mismo `client_operation_id` rechazado antes del despliegue puede crear la
   venta después y que un reintento de una venta creada devuelve el mismo
   ticket, sin duplicar líneas, tenders ni stock.
5. Confirmar que el arqueo B8 usa sólo los tenders `cash`, incluso en ventas
   offline que llegan después del cierre, y que una anulación completa retira
   de caja y deuda todos los pagos del ticket.

## Entrega 3 — pantalla de caja

- `SaleState` usa filas de pago `{id, method, amountCents}`. En modo de un solo
  pago la única fila sigue automáticamente el total y `buildPayload` genera el
  mismo JSON que hoy. Al dividir, los importes pasan a ser explícitos: agregar
  un medio puede sugerir el remanente positivo, pero un cambio posterior de
  productos, cantidades, precios o descuentos **no redistribuye** dinero en
  silencio; muestra «Falta asignar» o «Sobra» y bloquea Cobrar hasta corregir.
  Al volver a una sola fila se restaura el seguimiento automático del total.
- `cannotCharge` trabaja en centavos: al menos un pago, cada importe > 0,
  suma exacta al total, cliente si hay `account`, y efectivo recibido válido y
  no menor que la suma `cash`. Al retirar el último pago `cash`, limpiar
  `cashReceived` y enviar `cash_received_ars: null`. Si cambia el precio y el
  servidor responde `TENDERS_MISMATCH`, conservar la distribución visible y
  exigir una corrección; no cobrar automáticamente otra vez.
- `PaymentPanel` ofrece «Dividir pago», edición y eliminación de filas, saldo
  por asignar, vuelto calculado sólo sobre efectivo y selección de cliente si
  alguna fila es fiado. Usa una lista **explícita** de métodos permitidos, que
  respeta el permiso actual de fiado. No deriva opciones con `Object.entries`
  de un mapa de etiquetas de lectura.
- `mixed` necesita una etiqueta de **visualización** en `/sales` y los informes,
  pero no se agrega a `PAYMENT_LABELS` ni `PAYMENT_METHOD_LABELS` mientras esas
  constantes generen opciones de formularios: `/sales` y Proveedores lo
  ofrecerían como entrada inválida. Usar `paymentLabel('mixed')` de sólo lectura
  o separar explícitamente etiquetas y opciones. El ticket sigue mostrando los
  métodos reales de los tenders, nunca «Mixto» como si fuera un pago.
- `PendingOperationsPanel` muestra la suma de todos los tenders. La cola B7
  reenvía el `payload` congelado de IndexedDB sin reconstruirlo; una operación
  antigua de un solo pago conserva su huella. El comprobante provisional y el
  definitivo muestran los N importes y el vuelto correcto.
- B9 no agrega búsqueda offline de clientes. Tampoco afirmar que «fiado sin red
  es imposible»: hoy una operación con cliente ya seleccionado puede encolarse
  tras perder conexión. Mantenerla visible hasta que el servidor confirme o
  exija revisión; si el producto exige prohibir incluso ese caso, diseñar una
  autorización previa específica en otro cambio, sin descartar un cobro hecho.

## Pruebas de aceptación

La matriz mínima usa importes exactos, no sólo que cierre una igualdad global:

| Caso | Resultado esperado |
| --- | --- |
| Tres líneas, $600 cash + $400 account, cliente real | Ventas $1.000; efectivo $600; deuda $400; dos tenders, sin multiplicación por líneas. |
| $600 cash + $400 debit_card, entregado $1.000 | Vuelto $400; arqueo efectivo $600; débito $400. |
| $500 cash + $500 credit_card | Liquidez $500; diferido $500; ventas $1.000. |
| Dos tenders cash de $300 y $700 | Líneas `cash`; un único importe de $1.000, no población B. |
| Sólo fiado mixto para un cliente sin otras operaciones | Aparece en listado de deudores, saldo individual y saldo masivo. |
| Fiado $400 y cobro posterior $400 | Saldo $0, nunca −$400. |
| Anulación, reintento y otro tenant | Cero efecto duplicado; ninguna fuga entre tenants; ticket anulable y reimprimible. |
| POS con línea DEMO o fuera de período | Igual filtro de procedencia/fecha en ventas y tenders; ningún importe huérfano. |
| Cambio de precio tras dividir | Se ve el nuevo total y el reparto incompleto; no se envía otro cobro hasta corregir. |
| Venta antigua en IndexedDB | Reintento idéntico; no se rehace el payload ni aparece `mixed`. |

Cubrir consultas reales en SQLite y PostgreSQL, incluyendo `func.date`, el
`UNION ALL`/scope compartido y una operación de varias líneas. Los tests de
FactsService deben comprobar por separado V, I, F, D y L; un invariante que
cierra solo puede ocultar que efectivo y fiado están mal a la vez. Ejecutar
tests focalizados, `make check`, cobertura, suite PostgreSQL y
`type-check`/lint/Jest/build del frontend. No se necesitan mutation tests por
cada lector si las regresiones verifican sus importes exactos y fallan al
retirar su rama B; hacer esa comprobación donde aporte evidencia.

Tras desplegar Entrega 2 y 3, usar un tenant de prueba controlado para crear
un mixto por API y por `/pos`, reintentar el mismo ID, consultar saldo,
`/insights/cash-breakdown` y arqueo, y anularlo. Un 401 en la ruta demuestra
registro y autenticación; no valida la imputación. La habilitación se considera
terminada cuando la matriz anterior pasa en backend y frontend y el recorrido
con datos de prueba reproduce los mismos importes.

## Fuera de alcance

No se procesa el pago externo, no hay devolución parcial de un ticket y un
`inflow` posterior de fiado sigue sin registrar el medio por el que entró
(limitación anterior a B9). La estadística `ticket_promedio` de FactsService
sigue contando `SaleEntry`, no tickets POS; corregir esa semántica requiere un
cambio propio y no debe confundirse con la distribución de pagos.

## Notas de verificación (contra el código, 2026-09-28)

- **La precondición de B7 ya está cumplida.** Las dos regresiones son exactamente
  lo que arregló el hotfix PR #67 (`274dc731`, en `origin/main` y en producción):
  la migración de B13 reclama con el payload original y sólo lo adapta ante
  `INSUFFICIENT_STOCK`, y un rechazo corregible deja la operación en
  `EN_CORRECCION` en vez de borrarla. Antes de quitar el guard alcanza con
  confirmar que sus tests de regresión siguen en verde; no hay trabajo nuevo.
- **No existe un tope de crédito que «mantener».** `credit_limit` sólo se lee en
  `GET /customers/{id}/balance` (`over_limit`, informativo); ni `pos.py` ni
  `sales.py` lo validan al vender. El punto 2 de la Entrega 2 queda en: probar
  que la deuda nueva suma sólo la parte `account`, y que `over_limit` se calcula
  con ese importe. Hacer cumplir el tope es un cambio de producto aparte.
- **El documento `docs/plans/b9-pago-mixto.md` está sin commitear** (untracked
  en `feat/pos-b8`). Va en el primer commit de la Entrega 1, en su rama propia
  desde `origin/main`.
