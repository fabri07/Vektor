# Plan revisado (v3): POS con errores veraces y preservación de la venta pendiente

## Contexto

En la prueba del POS en "Limpieza del Sur" (dueño, sin caja habilitada), la venta de $1.500 quedó en la cola local con el mensaje "Sin conexión" aunque había red. Además, el dueño quedó afuera por el PIN y aparecieron mensajes con causas inventadas. Hubo dos revisiones externas: la primera pidió diagnosticar antes de arreglar; esta versión incorpora la segunda.

### Hallazgo verificado: CORS no permite los headers propios del POS

`backend/app/main.py:90` (`allow_headers`) incluye Authorization, Content-Type, Accept, X-Request-ID, X-Trace-Id, sentry-trace y baggage. **No incluye `X-POS-Offline` ni `X-POS-Terminal`.** Además, `frontend/next.config.ts` no tiene rewrites, así que la API es cross-origin y cada header propio dispara un preflight.

- **Reintentos de la cola** (`posService.syncOperation`, `pos.service.ts:170`): siempre mandan `X-POS-Offline: 1`. Con esta configuración y despliegue cross-origin, el preflight se rechaza y el POST no sale. Axios devuelve un error sin `response`, `posErrorInfo` lo marca `status: null` y la pantalla dice "Sin conexión". **Mientras el despliegue sea cross-origin con esta configuración, ninguna venta encolada puede subir.** El alcance en producción queda pendiente de confirmar con un preflight real (paso 1).
- **PC con caja habilitada**: el interceptor (`frontend/src/lib/api.ts:29`) agrega `X-POS-Terminal` a todo `/pos/*`, así que tendrían el mismo problema.
- **Primer envío** (`createOperation`, sin headers propios): pasa el preflight. Por qué tardó ~15 s (el `timeout` de axios) **sigue sin explicarse**.
- **Defecto independiente**: el 500 de `unhandled_exception_handler` (`main.py:269`) se arma fuera de `CORSMiddleware`, así que todo 500 también llega como "sin red". Y como `X-Trace-Id` no llega expuesto en ese 500, el navegador no puede correlacionarlo.
- **PIN cancelado, verificado en el código**: `pinGateStore.cancel()` rechaza con `Error("PIN_CANCELLED")`, pero `api.ts:100-103` lo descarta y devuelve el 428 original. Por eso `TerminalsSection` no puede distinguir una cancelación y cae en "¿nombre repetido?".

## Orden

### 1. Diagnosticar y preservar la venta pendiente (sin tocar código)
- **No borrar los datos del sitio** en ese navegador: la venta sólo existe en IndexedDB.
- Correlacionar el primer POST con Railway/Sentry por `X-Trace-Id` (breadcrumb del front) o por `client_operation_id`, y contestar: **¿la venta existe en el servidor?** El timeout no dice si entró.
- Consulta read-only en Neon (la corrés vos, con la URL en tu shell): `pos_operations` por `client_operation_id`, sus `inventory_movements` y el stock del producto.
- Preflight real contra producción, sin token: `curl -i -X OPTIONS <api>/api/v1/pos/operations -H "Origin: <front>" -H "Access-Control-Request-Method: POST" -H "Access-Control-Request-Headers: x-pos-offline"`. Así se confirma el alcance del hallazgo.
- La **recuperación** ocurre recién después del deploy del paso 2: la cola la reintenta sola con el mismo id y el mismo payload.

### 2. CORS, clasificación de errores y trazabilidad (rama `fix/pos-cors-errores-veraces`)

**Backend (`main.py`)**
- Una constante con los headers propios (`X-POS-Offline`, `X-POS-Terminal` y los que ya están) usada en `allow_headers`. Mantener `X-Trace-Id` en `expose_headers`.
- 500 con CORS: el handler global corre en `ServerErrorMiddleware`, que está **afuera** de todo `add_middleware`, así que un middleware no lo cubre. Opciones: que el handler copie los headers CORS usando la misma regla de origen (lista + regex) y además agregue `X-Trace-Id`, o un middleware ASGI propio que capture la excepción dentro de CORS y convierta a 500. Cualquiera de las dos **se valida contra un 500 real**, no contra una suposición.

**Tests HTTP backend** (`backend/app/tests/api/`)
- Preflight `OPTIONS /api/v1/pos/operations` desde un origen permitido con `x-pos-offline, x-pos-terminal` → 200 y los headers permitidos.
- Mismo preflight desde un **origen no permitido** → sin `access-control-allow-origin`.
- Una ruta de prueba que lanza una excepción → 500 con `access-control-allow-origin` (origen permitido), `X-Trace-Id` presente y expuesto, y sin CORS para un origen no permitido.
- Mutaciones: sacar un header de la lista, o sacar el arreglo del 500, y confirmar que el test falla.

**Prueba de idempotencia (backend)**
- Simular que el servidor guarda la venta y se pierde la respuesta: primer POST commiteado y respuesta descartada.
- Reintentar con el **mismo `client_operation_id` y el mismo payload**, con `X-POS-Offline: 1`.
- Afirmar que vuelve el ticket original (mismo id), que hay una sola `pos_operation` y un solo movimiento de stock. Si existe un test equivalente de B2, se extiende al camino con header offline en vez de duplicarlo.

**Front: clasificación** (`lib/pos/errors.ts`, `lib/pos/syncClassification.ts`, `usePosSync.ts`, `PosScreen.tsx`, `PendingOperationsPanel.tsx`, `lib/offline/posDb.ts`)
- `PosErrorInfo` gana un `tipo`, con estos casos:
  - `respuesta`: hay status.
  - `timeout`: `ECONNABORTED`/`ETIMEDOUT`.
  - `cancelada`: `axios.isCancel`.
  - `transporte`: axios sin respuesta. Incluye red caída, DNS y CORS, que el navegador no deja distinguir.
  - `local`: no es un error de axios.
- Textos: "sin conexión" sólo cuando además `navigator.onLine === false`. En los demás casos sin respuesta: "El servidor no respondió (timeout)" o "No se pudo contactar al servidor", según corresponda. Nunca se afirma la causa.
- `local`: va a `REQUIERE_REVISION` con el mensaje y un evento en Sentry, **sin reintento automático**. Una excepción de JS se repite igual en cada intento y no la arregla la red. Se puede reintentar a mano desde el panel.
- `cancelada`: no cuenta como intento y no cambia el mensaje.
- `timeout` y `transporte`: siguen sin gastar intentos (la lección de B0 se mantiene), pero guardan `tipo` y trace id.
- Campo opcional nuevo en la cola (`ultimoTipo`, `ultimoTraceId`). Al ser opcional, no hace falta subir la versión de IndexedDB. El panel muestra una referencia corta (`ref 3f2a…`), y el aviso de encolado pasa a ámbar.

**Doc en el mismo commit:** `docs/claude/pos-offline.md`. Cubre los headers propios en CORS, el 500 con CORS y la tabla de clasificación de errores.

### 3. PIN: cancelación, recuperación y estado de carga
- `api.ts` + `pinGateStore.ts`: al cancelar, rechazar con un error tipado `PIN_CANCELLED` (exportar un helper `esPinCancelado(e)`) en vez del 428 original. Revisar los demás llamadores que hoy asumen el 428.
- Recuperación (`SecurityPanel.tsx` y el modal del PIN): «¿Olvidaste tu PIN?» con contraseña, usando `securityService.resetPin` (`services/security.service.ts:38`). Después del reset se **verifica el PIN nuevo** (`/auth/pin/verify`), se resuelve el gate y la acción pendiente se retoma **una sola vez**: el flag `_pinRetry` ya lo garantiza, y se agrega un test.
- La tarjeta tiene tres estados: cargando (esqueleto), error de carga (mensaje + reintentar) y cargado. Hoy `!status?.pin_set` muestra "no configuraste" mientras carga.
- `TerminalsSection.tsx:70`: si se canceló el PIN no muestra nada; en otro caso, el mensaje real del servidor.
- Tests jest:
  - cancelar → sin toast de error;
  - reset → verify → la acción se reintenta exactamente una vez;
  - estados de la tarjeta;
  - error real en `TerminalsSection`.

### 4. Checklist de caja completo, con criterios corregidos
1. **Turno y arqueo**: fondo $10.000. Ventas:
   - venta 1: efectivo $1.500 con vuelto;
   - venta 2: tarjeta $4.500;
   - venta 3: mixta $12.500 ($5.000 en efectivo + $7.500 por transferencia).

   Cerrar sin anular. Esperado = $16.500 (fondo + sólo efectivo); contado $15.500; diferencia −$1.000.
2. **Ticket**: imprimir al cobrar y «Reimprimir» desde «Últimos tickets».
3. **Anulación antes del cierre** (turno aparte): anular la mixta con motivo y PIN → vuelve el stock y el esperado baja a $11.500. **Anulación después del cierre**: caso separado. Si no hay una política explícita sobre su efecto contable, queda como hallazgo.
4. **Cajero**:
   - sólo ve la caja, y cualquier otra ruta lo lleva a `/pos`;
   - puede cobrar, pero no puede anular el ticket de otro;
   - si se le quita un permiso en Equipo, el control desaparece en la próxima acción;
   - cobra sólo desde una PC habilitada.
5. **Sin red**: con el WiFi cortado, cobrar dos ventas → sale el ticket «PENDIENTE DE SINCRONIZAR» (se prueba aunque la venta no esté en el servidor) y el panel dice «2 sin sincronizar». Al reconectar suben una sola vez; verificarlo en `/sales`.
6. **Números**: las mixtas se reparten por medio de pago y nunca aparecen como «Mixto», en `/sales`, en el arqueo diario y en `/dashboard`. Las anuladas no suman en ninguno de los tres.
7. **Pulido** (después de lo de fondo):
   - limpiar el aviso al agregar desde el buscador;
   - un escaneo sin match ofrece buscar por nombre y no culpa al encargado;
   - el default de «Imprimir al cobrar» lo decidís vos.

### 5. Ingesta (seguimiento; este plan no lo desarrolla)
- #81: lo mergea la otra sesión con CI verde y ahí para. Se conserva el test que prueba que sin aceptación no se guarda nada, con su mutación.
- Siguen en pie las soluciones del primer plan de ingesta (fórmulas, JSONB, clasificación), los pasos 2–9 de #48 y el paso nuevo de preservar ediciones sin depender de la posición de la fila.
- El import de Decoración con el ASTERIA modificado no se atribuye a #48 hasta tener el resultado concreto.

## Archivos críticos
- Backend: `backend/app/main.py` y tests nuevos en `backend/app/tests/api/` (CORS, 500, idempotencia con header offline).
- Front, POS: `src/lib/pos/errors.ts`, `src/lib/pos/syncClassification.ts`, `src/features/pos/usePosSync.ts`, `PosScreen.tsx` (`avisoDeEncolado`, `send`), `PendingOperationsPanel.tsx`, `src/lib/offline/posDb.ts`.
- Front, PIN: `src/lib/api.ts`, `src/stores/pinGateStore.ts`, el modal del PIN, `src/features/settings/components/SecurityPanel.tsx`, `TerminalsSection.tsx`.
- Doc: `docs/claude/pos-offline.md`.

## Verificación
- Backend: tests nuevos con `make test-file`, después `ruff check .` + mypy, como en CI. Suite completa y `-m postgres -n 0` contra `vektor_pgtest` antes del push, porque toca idempotencia.
- Frontend: `npm run type-check && npm run lint` y `npx jest --runInBand` de los archivos tocados.
- Producción, después del deploy:
  - el preflight del paso 1 ahora devuelve `x-pos-offline` permitido. Eso prueba que corre el código nuevo;
  - la venta de $1.500 sube sola;
  - en Neon hay una sola venta y un solo movimiento de stock para ese `client_operation_id`;
  - después, el checklist del paso 4 completo.
