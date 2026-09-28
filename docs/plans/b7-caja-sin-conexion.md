# B7 — La caja sin conexión

## Contexto

B13 dejó la caja cobrando, pero **sin red no vende**: `PosScreen` mandaba el
POST y, si no había respuesta, el carrito quedaba bloqueado "en duda" con un
único resguardo en `localStorage` (`lib/pos/pendingOperation.ts`: una sola
operación, escritura silenciosa que si fallaba dejaba mandar el cobro igual).
Un corte a media tarde dejaba al negocio sin poder cobrar — lo que el plan
maestro declara inaceptable: *"si Véktor se cae, el negocio no vende"*.

Se parte en dos: **B7a** (vender con la pestaña abierta, este documento) y
**B7b** (abrir `/pos` con la PC reiniciada y sin red, service worker).

## Las decisiones

### 1. Escribir antes de mandar, y si no se puede escribir, no cobrar

La operación, sus pagos y su comprobante provisional se escriben en **una**
transacción de IndexedDB **antes** del primer envío (`lib/offline/posDb.ts`).
Si el `put` falla, la caja lo dice y **no envía**: avisar es mejor que cobrar
algo que puede desaparecer. Antes, `savePending` se tragaba el error y el cobro
salía igual, así que un cierre del navegador con la respuesta en vuelo dejaba la
venta en ningún lado.

### 2. Sin respuesta, el carrito se LIBERA

El estado `en_duda` desapareció. Una venta sin respuesta pasa a la cola, sale su
comprobante provisional y el cajero atiende al cliente siguiente. Trabar la caja
esperando una confirmación es exactamente lo que B7 viene a evitar, y es seguro
porque cada venta sale con su `client_operation_id` y reenviarla devuelve el
ticket original (B2).

### 3. Una sola clasificación de las respuestas, por código

`lib/pos/syncClassification.ts`: `sincronizada | reintento | autenticacion |
revision`. `isDefinitiveRejection` (`cart.ts`) queda para el **primer envío
sincrónico** —carrito bloqueado, sin entrega todavía— y no se usa en la cola:
trata como definitivos el **402** (suscripción) y el **423** (mantenimiento),
que son justo los dos que se arreglan solos.

**Borrar sólo es posible en ese primer envío.** Desde que la venta pasa a
`PENDIENTE`, ninguna respuesta del servidor la borra: a lo sumo cambia de
estado. El único borrado posterior es el descarte manual con confirmación.

### 4. Pertenencia: tenant, usuario y caja

Sólo se sincroniza un pendiente cuyos tres coinciden con la sesión. Una venta
que cobró Juan espera a que entre Juan; subirla bajo la sesión de Ana se la
adjudicaría a Ana y el arqueo quedaría mal para los dos.

### 5. Un fallo de red no gasta presupuesto de reintentos

`navigator.onLine` da `true` con un router sin internet (lección de B0). Si cada
intento contara, unos minutos de corte agotarían el tope y la primera respuesta
real llegaría con la venta ya marcada como problemática.

### 6. El stock estimado es DERIVADO, nunca acumulado

`stockLocal = stock del servidor − unidades de las ventas todavía no
sincronizadas`. Con el descuento acumulado en el producto guardado **y** la
resta al sincronizar el catálogo, la misma venta se contaría dos veces. Una sola
fórmula, un solo lugar (`catalogDb.catalogoDeCaja`).

### 7. El catálogo se reemplaza entero

Se baja completo por cursor (`/pos/catalog` pagina por id justamente para esto)
y lo que no vino se borra: así las bajas se reflejan solas, sin que el servidor
tenga que llevar una lista de borrados. Si una página falla no se borra nada:
mejor un catálogo viejo completo que uno nuevo a medias.

### 8. El escaneo offline es un puerto, con corpus compartido

`lib/pos/scanCodeLocal.ts` porta `domain/scan_code.py` + `find_by_scan`. El
riesgo es concreto: una implementación más laxa cobraría un producto que el
servidor habría marcado ambiguo. `shared/pos/scan_corpus.json` lo leen los dos
tests, y `shared/**` entró a los triggers de los dos CI.

### 9. Una sola pestaña por PC

Web Locks (`lib/offline/tabLock.ts`). Dos pestañas sobre la misma cola se pisan
el estado. Sin `navigator.locks` **no bloquea**: una caja que no abre es peor
que un riesgo conocido.

## Lo que cambió del plan

- **No existe `try_decrement_for_sale`.** Quién no entra se decide una sola vez,
  en el bucle de precios con la fila bloqueada; un segundo lugar preguntando
  "¿alcanza?" es un segundo lugar del que puede divergir.
- **`tomarCandadoDeCaja` no puede `await` el `request()` de Web Locks**: esa
  promesa resuelve recién cuando termina el callback, y el callback tiene que
  seguir corriendo para que el candado siga tomado. Se avisa desde adentro.
- **`jest.setup.ts` necesita `structuredClone`**: jsdom no lo trae y IndexedDB
  lo usa para guardar. Se polyfillea con el serializador de V8 y no con
  `JSON.parse(JSON.stringify(...))`, que perdería `Date` y `Map`.

## Fuera de alcance, declarado

Fiado y aprendizaje de códigos sin red (los dos necesitan el servidor); pago
mixto (B9); sesiones de caja (B8) — B7 sólo conserva `client_session_id` para
que B8 pueda atribuir lo que sincroniza después del arqueo; cambio manual de
precio desde la caja (permiso y auditoría propios); el `@page` definitivo (B1).

## Verificación

`npm run type-check && npm run lint && npm run test && npm run build`, cada uno
con su exit code. Casos: la venta está en IndexedDB **antes** del primer POST y
si el `put` tira **no se envía**; el carrito se libera; el pendiente de otro
tenant/usuario/caja no se sincroniza; 402 y 423 son reintento y no revisión; un
error de red no gasta reintentos; nada se borra por una respuesta; el catálogo
completo por cursor y sus bajas; el estimado no se cuenta dos veces; el corpus
de escaneo contra el puerto; el comprobante provisional; la migración desde
`vektor-pos-pending-operation`; dos pestañas.
