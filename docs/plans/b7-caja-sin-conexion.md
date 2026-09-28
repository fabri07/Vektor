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

---

## B7b — abrir la caja sin internet

Service worker escrito a mano (`frontend/public/sw.js`), sin librerías, para que
`/pos` abra con la PC reiniciada y sin red.

### El interruptor es por PC, no una variable de entorno

`NEXT_PUBLIC_*` se **inlinea en el build**: apagarlo exigiría recompilar y
desplegar, que es justo lo que un kill switch no puede necesitar. La casilla
«Preparar esta PC para abrir sin internet» (sólo OWNER, en la barra de la caja)
vive en `posSwStore`. Apagarla **desregistra y borra la caché** en el acto, y
una PC que nunca la encendió no tiene service worker: el radio de daño es una
caja, no la app entera.

### Qué cachea, y sobre todo qué NO

`decidirRuta()` existe una sola vez, dentro de `sw.js`, y el test la **lee de
ese archivo** con `vm`: una copia "para testear" se separaría justo en el caso
que importa. Sólo dos cosas se interceptan —`/_next/static/**` cache-first
(llevan hash en el nombre) y la **navegación** a `/pos` network-first con
respaldo—. Se ignoran la API (`/api/**` y cualquier otro origen), los payloads
de **RSC** (header `RSC` o `?_rsc=`) y cualquier otra ruta.

### Activación en momento seguro

Nunca `skipWaiting()` solo: una versión nueva activada en medio de un cobro
mezcla código nuevo con página vieja. La caja manda `SKIP_WAITING` con el
carrito vacío y sin envíos en vuelo.

### Cómo se prueba en el local (es el paso 1 de B1)

1. Abrir `/pos` **con internet** en la PC que va a ser la caja y encender la
   casilla. Instalar la app (Chrome → ⋮ → Instalar).
2. Cobrar una venta de prueba y anotar el ticket.
3. Cortar la red **de verdad** (router apagado o adaptador deshabilitado; el
   checkbox "Offline" de devtools no vale).
4. **Reiniciar** la PC. No suspender ni hibernar, y en Windows 10/11 "Apagar"
   con inicio rápido es una hibernación del kernel: usar **Reiniciar**.
5. Encender, seguir sin red, abrir la app **desde su ícono**.

**Pasa** si abre y la venta pendiente sigue ahí. **Falla** si aparece el
dinosaurio, una pantalla en blanco, o abre con la cola vacía. En la consola,
`await navigator.storage.persisted()` dice si Chrome concedió persistencia: si
da `false`, la exportación de pendientes deja de ser el último recurso.

Hasta que ese paso pase, B7b **no está probado contra hardware real**: por eso
nace apagado.

### Límite declarado

La caché sólo se limpia al cambiar el nombre de `CACHE`, así que los chunks
viejos se acumulan. Con hash en el nombre son inertes; limpiarlos por antigüedad
es trabajo aparte.
