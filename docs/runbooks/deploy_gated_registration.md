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

### Procedimiento

1. Deployar `vektor-worker` y esperar a que esté sano.
2. Deployar `vektor-api`. El `preDeployCommand` (`sh scripts/migrate.sh` →
   `alembic upgrade head`) corre en un contenedor one-off **antes** de que la
   versión nueva reciba tráfico; si una migración falla, Railway aborta el deploy
   y la versión vieja sigue sirviendo.
3. Post-deploy, verificar que no quedaron solicitudes huérfanas en la ventana:

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
  quedaba `disabled` mientras faltara una respuesta, y Chrome no dispara envío
  implícito con el submit deshabilitado: el resumen de faltantes, el foco
  automático y los errores de los nueve grupos eran código muerto. El visitante
  llegaba al último scroll, no podía mandar nada y **el abandono resultante era
  invisible**. → *pendiente de commit*
- **P0 — un campo financiero vacío del onboarding se guardaba como `$0`.** →
  *pendiente de commit*

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
