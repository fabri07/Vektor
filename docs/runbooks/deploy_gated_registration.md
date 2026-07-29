# Runbook — Despliegue del acceso restringido (gated registration)

Guía operativa del deploy que cierra el registro abierto y lo reemplaza por una
**cola de solicitudes con aprobación manual**. Cubre el orden de despliegue, el
rollback, la reversa de migraciones y la deuda que entró a propósito.

Leer **antes** de deployar. Dos de las cosas que están acá no se descubren
mirando el código, y una de ellas falla en silencio.

---

## 1. Orden de deploy — el worker va PRIMERO

> **Desplegar `vektor-worker` ANTES que `vektor-api`.**

`vektor-api` y `vektor-worker` son servicios Railway separados y sus deploys
**no** están sincronizados. Este release agrega cuatro tareas Celery nuevas
(`backend/app/jobs/access_request_worker.py`):

- `jobs.notify_access_request_verification` — el mail de doble opt-in
- `jobs.notify_access_request_owner` — aviso al dueño de que hay una solicitud
- `jobs.notify_access_request_decision` — aprobada / rechazada / postergada
- `jobs.notify_account_exists` — alguien pidió acceso con un email que ya tiene cuenta

Si la API nueva sale primero, encola esas tareas contra un worker que todavía
corre el bundle viejo y **no las tiene registradas**. Celery descarta el mensaje
sin reintento.

**Por qué importa más de lo que parece.** Son dos columnas distintas de
`access_requests` y las dos quedan en un valor que nadie mira:

- `status` = `unverified` — el solicitante nunca recibió el mail, nunca pudo
  confirmar, y la solicitud jamás pasa a `pending` (que es donde el dueño la
  revisaría).
- `verification_email_status` = `pending` — **su valor por default**. El envío
  nunca se intentó, así que tampoco llegó a marcarse `failed`.

Ese segundo punto es el que duele: `--email-failed` filtra por
`verification_email_status` / `owner_notification_status` /
`decision_email_status` en `failed`. Un mail que nunca se intentó no está
`failed`, está `pending`. Por eso tampoco aparece acá:

```bash
python backend/scripts/access_requests.py list --email-failed
```

Es decir: **la ventana de deploy produce solicitantes perdidos que ninguna
herramienta de la casa muestra.** No hay alerta, no hay cola de fallos, no hay
rastro operativo. La única forma de encontrarlos después es listar a mano las
solicitudes `unverified` creadas dentro de la ventana.

### ⚠️ Por default Railway NO respeta ningún orden

Railway no tiene dependencias entre servicios. `vektor-api` y `vektor-worker`
observan el mismo repo y **los dos redespliegan en paralelo** apenas el merge
toca `main`. Ninguno de los dos `railway.toml` declara `watchPatterns` ni
prioridad, así que "deployar el worker primero" **no es una acción que exista
por default**: hay que fabricarla apagando el automático del otro.

Existe una mitigación accidental —`vektor-api` tiene `preDeployCommand` (las
migraciones) y el worker no, así que la API tarda más en recibir tráfico y el
worker suele ganar la carrera—, pero **no alcanza como garantía**: los tiempos de
build varían, y si el build del worker falla, la API queda viva contra un worker
viejo por tiempo indefinido.

### Procedimiento

1. **Antes de mergear**, en el servicio **`vektor-api`** de Railway: apagar los
   deploys automáticos (está en Settings, en la sección del repo conectado; el
   rótulo cambió entre versiones de la UI — buscar el toggle de deploys
   automáticos / "Wait for CI"). No tocar nada en `vektor-worker`.
2. Mergear el PR. Ahora **solo redespliega `vektor-worker`**.
3. Esperar a que el worker quede **Active** y revisar sus logs: tiene que
   arrancar sin `ImportError` y registrar las cuatro tareas nuevas. Recién
   entonces seguir.
4. En `vektor-api`, disparar el deploy **a mano** (Deploy / Redeploy sobre el
   último commit). El `preDeployCommand` (`sh scripts/migrate.sh` →
   `alembic upgrade head`) corre en un contenedor one-off **antes** de que la
   versión nueva reciba tráfico; si una migración falla, Railway aborta el deploy
   y la versión vieja sigue sirviendo.
5. Volver a **prender** los deploys automáticos de `vektor-api`. Si este paso se
   olvida, el próximo merge no despliega la API y el síntoma es confuso: código
   nuevo en `main`, worker actualizado, API vieja sirviendo.
6. Post-deploy, verificar que no quedaron solicitudes huérfanas en la ventana:

```bash
python backend/scripts/access_requests.py list --status unverified
python backend/scripts/access_requests.py list --email-failed
```

Para cualquiera que haya quedado esperando:

```bash
python backend/scripts/access_requests.py resend-invite <request_id> --apply
```

---

## 2. Migraciones

Tres revisiones, cadena lineal con un solo head:

```
20260805_0001 (F9a)
  └─ 20260806_0001_access_requests
       └─ 20260806_0002_unify_vertical_codes
            └─ 20260806_0003_audit_log_tenant_nullable   ← head
```

Las tres son reentrantes y las tres tienen `downgrade`.

### 🛑 Gate previo (M-2) — correr ANTES de desplegar

`20260806_0001` es idempotente **por inspección**:

```python
existing = set(inspector.get_table_names())
if "access_requests" not in existing:
    op.create_table(...)   # ← los 6 CHECKs y los 4 índices van ACÁ ADENTRO
```

Esa guarda protege contra el doble `upgrade head` de un mismo deploy (Railway
puede correr el `preDeployCommand` más de una vez), pero tiene un modo de falla
silencioso: si `access_requests` ya existiera por cualquier otro motivo, el
`create_table` **entero** se saltea —CHECKs, índices normales e índice único
parcial `uq_access_requests_open_email` incluidos— y la revisión **igual queda
marcada como aplicada** en `alembic_version`. Quedaría una tabla sin las
garantías que hacen que `'otros'` sea inescribible como vertical asignado y que
una solicitud aprobada no pueda quedarse sin vertical.

```bash
cd backend
export DATABASE_URL='...'          # Neon, desde tu shell — el script nunca la imprime
python scripts/preflight_access_requests_tables.py
```

Es read-only (7 sentencias, todas `SELECT`). Veredictos:

- **`✅ LIBRE`** — ninguna de las dos tablas existe → desplegar.
- **`🛑 BLOQUEADO`** — hay tabla residual. **No desplegar.** El script vuelca
  columnas, CHECKs, índices y conteo de filas para decidir a mano: dropear (si
  está vacía) o agregar los CHECKs faltantes a mano (si tiene datos reales).

Como esta rama nunca se desplegó, lo esperable es `LIBRE` — pero es una corrida
de una sola oportunidad: si no se pregunta acá, el modo de falla es silencioso.

### ⚠️ `alembic downgrade base` NO funciona

La reversa completa **rompe a mitad de cadena**. `20260806_0003` corre primero en
la reversa, cuando las filas de `decision_audit_log` con `tenant_id IS NULL`
todavía existen, y su `SET NOT NULL` falla.

Para resetear una base de desarrollo, **no** intentes bajar la cadena:

```sql
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
```

```bash
alembic upgrade head
```

**Nota más amplia:** CI nunca corre ningún `downgrade`. Las 77 revisiones del
proyecto tienen un camino de reversa que jamás se ejecutó. Este release no
empeora eso, pero es la primera vez que se documenta que la reversa completa está
efectivamente rota.

---

## 3. Rollback

El rollback funcional es de una línea, sin tocar la base:

```
ENABLE_OPEN_REGISTRATION=true
```

(`backend/app/config/settings.py:235`, default `False`.) Con el flag prendido,
`POST /auth/register` vuelve a crear cuentas. Las migraciones son additive y no
hace falta revertirlas — y dado el punto 2, tampoco conviene intentarlo.

`/register` sobrevive como redirect server-side a `/solicitar-acceso`, así que
links, bookmarks y CTAs viejos siguen funcionando en cualquiera de los dos modos.

---

## 4. Verificación post-deploy (smoke)

Camino completo, con una cuenta de prueba:

1. `/solicitar-acceso` → completar y enviar.
2. Llega el mail de doble opt-in → verificar.
3. `access_requests.py list` muestra la solicitud en la cola.
4. `access_requests.py approve <id> --vertical <rubro> --apply` → llega el mail de
   decisión y se acuñan Tenant + User + Subscription + BusinessProfile +
   MomentumProfile.
5. Definir contraseña y completar el onboarding **dejando los campos de plata
   vacíos**: tienen que quedar `NULL` en `business_profiles`, no `0`.

---

## 5. Deuda abierta

### Cerrado en este release

*(Sección viva: al cerrar cada ítem se anota el commit, no se borra la línea.)*

- **P0 — `/solicitar-acceso` perdía solicitudes en silencio.** El botón de envío
  quedaba `disabled` mientras faltara una respuesta, y el HTML Standard no
  dispara envío implícito cuando el botón por defecto existe y está
  deshabilitado: el resumen de faltantes, el foco automático y los errores de
  los nueve grupos eran código muerto. El visitante llegaba al último scroll, no
  podía mandar nada y **el abandono resultante era invisible**.
  → `579c03e1` (envío alcanzable + resumen anunciable) y `5b5c74f7` (ARIA de los
  nueve grupos; el rubro señalaba su error solo con un borde rojo).
- **P0 — un campo financiero vacío del onboarding se guardaba como `$0`.**
  `parseFloat(campo) || 0` convertía "no sé" en "no gasto nada", y el
  completeness sumaba 20 puntos de caja de forma incondicional.
  → `b45fbfb6` (cambio bilateral frontend/backend, sin migración: las columnas
  ya eran nullable).
- **WCAG AA.** CTA a 2,11:1 y `text-gray-400` del onboarding a 2,54:1; filas de
  opción a 38px. → `300dbd40`.
- **Cero persistencia del borrador** y contador de progreso. → `64b53e8d`.
- **`main_concern` se preguntaba dos veces** y la segunda respuesta pisaba la
  primera. → `6ad543f0`.
- **`<title>` genérico** en tres pantallas y cuatro copias del mismo shell.
  → `ccf71767`.

**Lo que este corte NO cerró y sigue abierto** (ver tabla de abajo): el salto de
tema oscuro→blanco, la reciprocidad, el recorte de preguntas, la migración de
tokens, y la telemetría de abandono — que sigue sin existir porque no hay
colector. El corte se llama "P0 y P1 técnicos priorizados", no "P0/P1 completo".

### Diferido a propósito

Cada ítem quedó afuera del corte con un motivo, no por olvido:

| Ítem | Motivo |
|---|---|
| **Telemetría de abandono + colector (GTM)** | No existe colector: `trackLandingEvent` (`frontend/src/lib/landingAnalytics.ts`) es un no-op si `window.dataLayer` no existe, y `dataLayer` solo se inicializa dentro de un test. Los cuatro eventos que el formulario ya emite **no llegan a ninguna parte**. Agregar eventos no resuelve nada por sí solo, y sí abre decisión sobre GTM, config de producción y revisión de privacidad (Ley 25.326). Además `visibilitychange → hidden` **no significa abandono**: cambiar de pestaña daría falsos positivos |
| **Reciprocidad al final del formulario** | La relación sigue siendo 13 preguntas a 0 devoluciones. Requiere decisión de producto sobre qué devolver sin inventar métricas con datos incompletos |
| **Recortar las 13 preguntas / la densidad de 35 opciones** | Decisión de producto: qué respuesta cambia realmente la decisión de aprobar, y qué se puede mover al onboarding |
| **Salto de tema oscuro→blanco a mitad del embudo** | Síntoma de los dos dialectos de tokens (ver §6); arrastra 163 archivos |
| **Migración global `vektor-*` / `vk-*`** | 55 archivos usan un dialecto, 108 el otro, más 67 grises crudos de Tailwind. Incremental por módulos, post-merge |
| **Decoración del panel lateral del embudo** (`DoodleCollage`, `CHECK_ITEMS`, trust band de `/login`) | Decisión de diseño, no fix técnico |
| **Matriz de regresión cross-browser, Lighthouse, lector de pantalla completo** | Compuerta de release aparte |

---

## 6. Dos advertencias para el que venga después

Ninguna de las dos es un bug abierto. Las dos hacen que un cambio "obvio" salga mal.

### 6.0 `vektor-teal` no sirve para texto

El teal de marca (`#27c7b8`) con texto blanco da **2,11:1**. Funciona para
superficies, iconos y acentos; no para texto de ningún tamaño. Para eso está
`vektor-teal-deep` (`#17776e`, mismo matiz al 60 % de brillo, 5,38:1), que es
lo que usan los CTA. `frontend/src/__tests__/contraste_tokens.test.ts` lo mide
en CI.

Quedan con el defecto, a propósito y fuera del embudo:
`DashboardLaunchpadNav` y `ManualEntryLauncher` (gradiente viejo con texto), y
`RiskCard` / `ActionCard` (`text-gray-400` sobre blanco). Están detrás del
login; cambiar el chrome del dashboard no era parte de este corte.

### 6.1 Las pantallas finales están a 4,71:1 — 0,21 puntos sobre AA

`/solicitud-enviada`, `/solicitud-verificada`, `/definir-password` y `/login`
usan `text-vk-text-muted` (`#64748b`) sobre `bg-vk-surface-w` (blanco puro).
El ratio medido es **4,71:1**, y el mínimo AA para texto normal es 4,5:1.

Cualquier ajuste que aclare ese token —aunque sea "un poquito", aunque sea para
"suavizar"— rompe la conformidad de cuatro pantallas de una. Si hay que tocarlo,
va para el lado oscuro y se vuelve a medir.

### 6.2 `tailwind.config.ts` y `globals.css` definen los mismos tokens con valores invertidos

`frontend/tailwind.config.ts` (bloque `vkColors`) y
`frontend/src/styles/globals.css` (bloque `:root`, "Backwards-compatible tokens")
declaran **los mismos nombres `vk-*` con valores opuestos**:

| Token | `tailwind.config.ts` | `globals.css` |
|---|---|---|
| `vk-bg-light` | `rgb(247 248 250)` — casi blanco | `var(--vektor-night)` `#050913` — casi negro |
| `vk-surface-w` | `rgb(255 255 255)` | `var(--vektor-ink)` `#0d1627` |
| `vk-border-w` | `rgb(229 233 240)` | `var(--vektor-border)` `#243246` |
| `vk-text-primary` | `rgb(15 22 35)` — casi negro | `var(--vektor-white)` `#f7fbff` |
| `vk-text-secondary` | `rgb(74 85 104)` | `var(--vektor-body)` `#d6e2f0` |

**Manda Tailwind.** Todo lo que se ve vía clases utilitarias (`bg-vk-bg-light`,
`text-vk-text-primary`, …) se genera desde `tailwind.config.ts` en build. Las
variables CSS de ese bloque son **inertes** para las utilidades: solo aplican
donde alguien escribe `var(--vk-…)` a mano, que son tres reglas en `globals.css`
(`html`, el gradiente del `body`, y el `color` base).

Consecuencia práctica: el `<html>`/`<body>` son oscuros por CSS mientras las
pantallas del embudo pintan encima superficies blancas desde Tailwind. Los dos
sistemas conviven por superposición, no por acuerdo.

**Editar `globals.css` para "arreglar el theme" no produce el efecto esperado.**
La unificación real es la migración de dialectos, que está en la tabla de
Diferido.

---

## Referencias

- CLI de la cola: `backend/scripts/access_requests.py`
  (`list`, `show`, `approve`, `reject`, `waitlist`, `otros`, `resend-invite`,
  `expire-stale`). Sin `--apply` es dry-run con rollback.
- Recuperación de la base sin OWNER: `backend/scripts/bootstrap_superadmin.py`.
- Tareas de mail: `backend/app/jobs/access_request_worker.py`.
- Servicio: `backend/app/application/services/access_request_service.py`.
