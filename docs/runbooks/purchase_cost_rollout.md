# Runbook — encender el motor de costos de compra por tenant

**Qué habilita:** el reparto del envío de un comprobante entre sus líneas y los
ajustes de costo por descuento/IVA/flete (F-H6.c + F-H6.d). Es la superficie que
mueve plata **sin que el usuario opte**: mapear una columna de descuento ya altera
el costo del producto.

**Qué NO habilita** (sale global, y está bien que salga global):

- F-H6.a — los targets nuevos del catálogo. Son inertes si nadie los mapea.
- F-H6.b — el cobro del envío del comprobante. No cobra nada sin una
  `ShippingDecision` explícita del usuario; no hay default, a propósito.
- El rechazo pre-lease de archivos planos con columnas de costo. No cobrar un
  envío mapeado es incorrecto con o sin compuerta.
- El arreglo del guard de reversa del borrado, que era un bug vivo.

---

## La variable

```
PURCHASE_COST_ROLLOUT_TENANT_IDS=<uuid>,<uuid>
```

- **Default vacío = nadie habilitado**, y el comportamiento es idéntico al de
  `origin/main`. Ese es el estado con el que se fusiona.
- Acepta csv o array JSON, con espacios.
- Un UUID malformado **se descarta con `logger.error` y la API arranca igual**.
  El fail-safe importa más que de costumbre: este repo no tiene staging, así que
  la variable se setea en producción desde el minuto cero y sin ensayo previo.

## Por qué la compuerta es barata

Casi todo se auto-gatea con un solo punto de control. Si el planificador de costos
no corre para un tenant:

- `costo_calculado` es `None` → el movimiento y el producto vuelven a recibir el
  mismo número, como antes;
- no se escribe `_vektor_costo_base` → la procedencia guardada es «no sé» → el
  guard de V5 dice «sí pisa» → comportamiento de hoy;
- ningún `ExpenseEntry` lleva `attributed_to_inventory` → el filtro de los
  agregados de resultado no encuentra nada que filtrar.

Los dos enforcement points son el endpoint de preview y la invocación del
planificador, y los dos están **en backend**: ocultar el control en el frontend no
alcanza.

---

## Procedimiento

1. **Fusionar con la lista vacía.** Verificar en el log del pre-deploy de Railway
   que `alembic upgrade head` no hizo nada (esta entrega no trae migraciones) y
   que `/health` responde. `vektor-api` y `vektor-worker` redespliegan en paralelo
   y sin orden garantizado.
2. **Encender UN tenant demo**, nunca una cuenta real. Setear la variable en el
   servicio `vektor-api` y esperar el redeploy.
3. **Probar el ciclo completo** sobre ese tenant, con un `.xlsx` que tenga:
   Ventas antes que Productos · una compra con envío repetido en varias líneas del
   mismo comprobante y `por_subtotal` elegido desde la pantalla · una venta
   anterior a su única compra · una venta cuyo producto no existe.
   Recorrer: preview de grupos → confirmación → agregados → caja → **DELETE**.
4. **Contrastar el preview con lo persistido.** Los números que mostró la pantalla
   tienen que ser exactamente los que quedaron. Hay un test que lo fija, pero es
   lo primero que hay que mirar en datos reales.
5. **Leer la traza** del tenant: `pipeline_events` (`STAGE_CONFIRM` trae el modo
   de reparto resuelto) y `decision_audit_log`.
6. **Ampliar de a un tenant.**

## Qué mirar para saber si algo salió mal

- Un `unit_cost_ars` que **bajó** sin que el proveedor bajara el precio: es el caso
  que V5 viene a evitar. Chequear `custom_fields._vektor_costo_base` del producto.
- `total_expenses` y `stock_value_ars` en la misma respuesta de
  `GET /economic-summary`: el flete repartido tiene que estar en UNO de los dos,
  nunca en los dos ni en ninguno.
- El arqueo tiene que seguir viendo el flete: si el gasto desapareció de la caja,
  el filtro de resultado se aplicó donde no iba.

## Cómo apagarlo

Sacar el tenant de la lista y redesplegar. **No revierte los datos ya importados**:
los costos que se calcularon con reparto quedan como quedaron, y su procedencia
también. Para deshacer un import puntual, borrar el archivo — la reversa ahora sí
devuelve `unit_cost_ars` y la procedencia.

## Antes de sacar la compuerta del todo

Tres cosas abiertas, cada una con su fase:

- **F-H6.f** — el camino plano no cobra el envío, descarta las decisiones de costo
  que vienen de la API (busca la clave `""` y el endpoint manda el `context_id`
  real) y no emite avisos. Hoy se **rechaza** ese archivo antes del lease en vez de
  importarlo mal.
- La reversa probada sobre datos preexistentes en producción, no sólo en tests.
- La salida de caja del flete de línea verificada contra un arqueo real.
