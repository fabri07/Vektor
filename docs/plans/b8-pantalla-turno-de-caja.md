# B8 del POS: la pantalla del turno de caja

## Por qué

El backend de B8 está en producción (PR #68, mig `20260929_0001`), pero ninguna pantalla lo usa, y la caja tiene un bug que lo ensucia: `PosScreen.tsx:182` crea el turno con `useRef(newOperationId())`, o sea **un turno nuevo en cada montaje**. Cada recarga de `/pos` que después vende hace que el servidor adopte otro turno abierto con fondo $0, y ninguno se cierra nunca.

El resultado buscado:
1. Un turno por jornada de cada cajero en cada caja, que sobrevive a una recarga.
2. La caja no cobra sin un turno abierto con el fondo declarado.
3. El cierre se hace con arqueo ciego.
4. El dueño puede cerrar sin arqueo los turnos basura que ya existen en producción.

Decisiones del usuario: **arqueo ciego**, **estado nuevo «sin arqueo» (con migración)** y **apertura obligatoria**.

Compatibilidad: es compatible con B9, que ya está en `origin/main` (`d98bb17d`). La cadena de migraciones termina en `20260929_0001`, así que la nueva no choca con nada.

## 0. Dónde se trabaja

Esta carpeta está en `feat/pos-b9` (ya mergeada) y hay otra sesión mirándola. Por eso el trabajo va en un **worktree aparte**, en la rama `feat/pos-b8-pantalla` creada desde `origin/main` actualizado. La carpeta principal no se toca. Antes de pushear: `git log origin/main..HEAD`.

## Backend

### Migración `20261001_0001` (aditiva, con los guards de E8c y el lock de `env.py`)

- `pos_cash_sessions.status`: el CHECK pasa a `OPEN | CLOSED | ABANDONED`. Se hace con `DROP CONSTRAINT IF EXISTS` + `ADD`, que es idempotente por construcción. Se actualiza también el `CheckConstraint` del modelo, porque SQLite lo lee de ahí. En el modelo se agrega `ESTADO_ABANDONADO`.
- Columna nueva `float_declared_at TIMESTAMPTZ NULL`, sin backfill.

### Servicio: `pos_cash_session_service.py`

**(A) Arreglo previo, un bug de B8 que ya está en producción.**
- En la carrera del UNIQUE, `abrir_o_adoptar` hace `await session.rollback()`.
- Cuando la llama `create_pos_operation`, eso pasa **después** de que ya se hizo flush de la venta, sus líneas, sus tenders y el reclamo de idempotencia. Si la venta pierde la carrera contra la apertura explícita, el rollback **se lleva puesta la venta**, y el handler sigue con objetos desasociados.
- El arreglo es crear el turno dentro de `guarded_savepoint` (`_savepoint.py`) clasificando sólo el UNIQUE `(tenant, client_session_id)`. Ante el conflicto se vuelve a leer y el resto de la transacción queda intacto.
- Test de Postgres con dos sesiones `NullPool`: venta y apertura simultáneas. Tienen que sobrevivir la venta y el turno. Para confirmar que el test prueba algo, se revierte el arreglo y tiene que fallar.

**(B) Declarar el fondo en los tres caminos y con candado** (el punto 1 de la revisión).
- `abrir_o_adoptar(..., fondo_declarado: Decimal | None)`: la ruta explícita pasa el fondo y la venta pasa `None`.
- Una vez obtenida la fila, por cualquiera de los tres caminos (ya existía, la creó, o perdió la carrera y volvió a leer), si `fondo_declarado is not None` se vuelve a leer con `SELECT … FOR UPDATE` + `populate_existing`, como en B11.
- Si la fila está OPEN y `float_declared_at IS NULL`, se fijan `opening_float_ars` y `float_declared_at`. Con el candado, dos aperturas simultáneas con fondos distintos se serializan: gana la primera y la segunda ve el fondo ya declarado.
- Un turno CLOSED o ABANDONED no se toca.

**(C) Una apertura con otro fondo no contesta como si hubiera funcionado** (el punto 2 de la revisión).
- Responde **el turno con el fondo real**. `PosCashSessionResponse` gana `float_declared: bool`.
- El hook toma el valor del servidor, nunca el que mandó. Si difiere, la barra avisa «El fondo registrado es $X».
- Se elige esto y no un 409 porque el conflicto sólo aparece entre dos cajas con el mismo id de turno, que es un bug de la caja. Un 409 dejaría al cajero trabado sin poder vender por un error que no es suyo.
- El reintento con el mismo cuerpo sigue siendo inocuo.

**(D) Abandonar.**
- `abandonar(session, sesion, *, user_id, reason)`: sólo sobre OPEN. Pone `status=ABANDONED`, `closed_at`, `closed_by_user_id` y `notes=reason`, y deja los tres importes en **NULL**. Si el turno ya estaba CLOSED o ABANDONED, levanta `SesionYaCerradaError`.
- Una venta que llega a un turno ABANDONED ya cae en tardías, porque `esta_abierto` es False.

### Rutas: `api/v1/pos.py`

- **`POST /pos/cash-sessions/{id}/abandon`**:
  - requiere `require_role(OWNER, ADMIN)` y `require_modify_access` (PIN);
  - `reason` obligatorio: `PosCashSessionAbandonRequest`, 422 `ABANDON_REASON_REQUIRED`;
  - `FOR UPDATE` sobre el turno y audita `POS_CASH_SESSION_ABANDONED`;
  - si el turno ya estaba cerrado, 409 `CASH_SESSION_ALREADY_CLOSED`;
  - **no** entra en `CASHIER_ALLOWED_ROUTES`;
  - **sin** gate de suscripción, porque es una corrección (política del bloque B de suscripciones).
- **Arqueo ciego del lado del servidor:** a un CASHIER, `GET /cash-sessions/{id}` le devuelve `expected_cash_now_ars = None`. Si el esperado sólo se ocultara en la pantalla, el cajero lo tendría a un request de distancia y el arqueo dejaría de ser ciego. OWNER y ADMIN lo siguen viendo. El resultado del cierre sí trae el esperado: ahí el conteo ya está firmado.
- `GET /pos/cash-sessions` gana el filtro opcional `terminal_id` (para el aviso de turnos anteriores) y el límite máximo sube a 200 (para limpiar producción).
- **Nota para B14:** `POST /cash-sessions` y `/close` hoy no tienen gate de suscripción, y así tiene que seguir. B14 los suma a `RUTAS_DE_CAJA_SIN_GATE`.

### Tests: `test_pos_cash_sessions.py` y los de Postgres

- Fondo con orden **venta → apertura** y con orden **apertura → venta**: los dos terminan con el fondo declarado, y el esperado lo incluye.
- La carrera real venta ∥ apertura en Postgres: sobreviven la venta y el fondo.
- Dos aperturas concurrentes con fondos distintos: queda una sola declaración, y las dos respuestas muestran el mismo fondo real.
- Una segunda apertura con otro fondo responde el fondo real y `float_declared=true`.
- Abandonar deja los importes en NULL y audita.
- Los 409 de abandonar dos veces y de abandonar un turno CLOSED.
- Abandonar sin PIN da 428, y el cajero recibe 403 (lo cubre el barrido de `test_cashier_access.py`).
- Una venta a un turno ABANDONED queda como tardía.
- Un cajero no recibe `expected_cash_now_ars` y el dueño sí.
- `test_migraciones_idempotentes_pg.py` y `test_migraciones_concurrentes_pg.py` con el head nuevo.

## Frontend

### Store: `stores/posCashSessionStore.ts`

- zustand `persist`, con clave propia que `logout` no borra, igual que `posTerminalStore`.
- Un turno por clave `tenantId:userId:terminalId|"sin-terminal"` con `{clientSessionId, serverId|null, openingFloatCents, openedAt, aperturaConfirmada}`.
- Otro cajero en la misma PC abre su propio turno: mismo criterio de pertenencia que B7a.

### Servicios: `services/pos.service.ts`

`openCashSession`, `listCashSessions({open, terminalId})`, `getCashSession`, `closeCashSession` y `abandonCashSession`, con el tipo `PosCashSession`.

### Hook: `features/pos/useCashSession.ts`

- Al montar, lee el turno guardado. Si tiene `serverId` y hay red, confirma el estado con el GET. Si está CLOSED o ABANDONED (lo cerró el encargado), borra el guardado y vuelve a la pantalla de apertura.
- Las ventas que ya estaban en la cola con el id viejo llegan como tardías. Es correcto y queda documentado.
- `abrir(fondoCents)`: genera el id y lo guarda **antes** de mandar. Si no hay red, `aperturaConfirmada=false` y la caja cobra igual.
- La apertura pendiente se reintenta en el mismo tick que `usePosSync.flush`. Con (A)+(B), el orden entre la apertura y las ventas ya no importa.
- Al confirmar la apertura se guardan el `serverId` y el fondo **del servidor**.

### Pantalla de caja

- **`PosScreen.tsx`:**
  - `useRef(newOperationId())` se reemplaza por el turno del hook;
  - sin turno se renderiza **`OpenCashSession.tsx`** (fondo inicial, se admite $0 declarado, parseo a centavos de `lib/pos/cart.ts`) y no se muestra el carrito;
  - el aviso «Tenés N turnos anteriores sin cerrar en esta caja — pedíselos al encargado» sale de `?open=true&terminal_id=`.
  - `PaymentPanel`, `cart.ts` y `PendingOperationsPanel` (los de B9) no se tocan.
- **`CashSessionBar.tsx`:** muestra «Turno HH:MM · fondo $X», un indicador si la apertura no está sincronizada o si el fondo del servidor difiere, y «Cerrar turno». **No muestra el esperado a un cajero.**

### Cierre: `CloseCashSession.tsx`, arqueo ciego

- **Requiere red.** Si la apertura no está confirmada, primero la confirma.
- Antes de pedir el conteo corre `flush()` y cuenta lo pendiente de **este** `client_session_id` **en dos grupos rotulados aparte**:
  - «N sin sincronizar»;
  - «M sin completar (en corrección)». Estas tienen `charged_at` afirmado, así que puede haber plata de ellas en el cajón. Es el punto 3 de la revisión.
- Si hay algo pendiente, avisa que no entra en este arqueo y que va a llegar como tardío, y deja confirmar igual.
- Pide «Efectivo contado» y notas. **No muestra el esperado.**
- Después de firmar muestra el resultado del servidor: fondo, esperado, contado y diferencia (sobrante o faltante). Después borra el turno guardado y vuelve a la apertura.
- **Respuesta perdida:** si el POST falla por red, al reintentar se hace primero el GET del turno. Si ya está CLOSED, se muestra ese resultado y no se manda otro conteo.
- Un 409 `CASH_SESSION_ALREADY_CLOSED` se trata igual: GET y mostrar lo firmado (puede haberlo cerrado el encargado).
- *Declarado sin hacer:* imprimir el comprobante de arqueo (se puede agregar sobre `lib/print`).

### Panel del encargado: `CashSessionsPanel.tsx` (OWNER y ADMIN, link «Turnos» en la barra)

- **Abiertos** de todos los cajeros: cajero, caja, apertura, fondo y si fue declarado, más el efectivo cobrado hasta ahora. Para los turnos basura de producción hay **selección múltiple** con «Cerrar sin arqueo» y un motivo común. Cada turno se cierra con su propio request; la ventana del PIN (10 min) cubre la tanda, y ante el 428 se abre `PinGateModal`.
- **Cerrados recientes** con su diferencia, y detalle con las ventas tardías: eso es la conciliación.
- Todo con tokens `pos-*`.

### Tests de jest (`features/pos/__tests__/`)

- Una recarga conserva el turno, y otro usuario en la misma PC no lo hereda.
- Sin turno no hay carrito.
- Apertura sin red: cobra, y la apertura se sincroniza después. El fondo del servidor que difiere se muestra.
- **Venta mixta en el turno** (el punto 4 de la revisión): el payload lleva el `client_session_id` del turno, y el esperado que se muestra en el resultado es el fondo más la parte en efectivo.
- El cierre no muestra el esperado antes de firmar, y sin red está deshabilitado.
- El aviso separa «sin sincronizar» de «en corrección».
- La respuesta perdida y el 409 muestran lo firmado sin reenviar.
- El panel exige motivo y la selección múltiple manda un request por turno.
- `pos_theme.test.tsx` sigue verde.

## Documentación (en el mismo commit que cada cambio)

- `CLAUDE.md`, bullet de B8:
  - el turno por montaje;
  - el rollback de la carrera y su arreglo;
  - `float_declared_at` y los tres caminos;
  - la respuesta con el fondo real;
  - ABANDONED;
  - el arqueo ciego también en el servidor;
  - la ruta nueva y la migración.
- Se quita «sesiones de caja (B8)» de los «Declarado sin hacer» de B13.
- En memoria, `project_vektor_pos_hardware.md`.

## Entregas (commits separados)

1. Backend, parte (A): arreglo del rollback en la carrera, con su test de Postgres. Es un bug que ya está en producción, así que va aislado.
2. Backend: migración, (B), (C), (D), el ciego del lado del servidor, los filtros y los tests.
3. Frontend: store, hook, apertura y barra. Con esto se corrige el bug del turno por montaje.
4. Frontend: cierre ciego y panel del encargado.

## Verificación

- Backend: `make test` completo; `pytest -m postgres -n 0` contra `vektor_pgtest`, exportando `DATABASE_URL` local además de `TEST_PG_DSN`; `ruff check .` y `mypy`.
- Frontend: `npm run type-check`, `lint`, `test -- --runInBand` y `build`.
- Smoke en un tenant demo con el stack `*-local`:
  1. abrir con $10.000;
  2. vender en efectivo y en mixto;
  3. recargar y verificar que sigue el mismo turno;
  4. cerrar a ciegas contando el esperado, con diferencia 0;
  5. desde el dueño, cerrar sin arqueo varios turnos viejos.
- Después del deploy:
  - sonda `POST /api/v1/pos/cash-sessions/{uuid}/abandon` sin token, que tiene que pasar de 404 a 401;
  - limpiar los turnos basura de producción desde el panel.
