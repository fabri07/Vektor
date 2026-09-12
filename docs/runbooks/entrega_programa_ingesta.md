# Entrega del programa de ingesta (E1–E8b)

**Qué es esto.** El plan concreto para poner en producción lo que la rama
`fix/ingestion-reliability-program` acumuló. No es una autorización: cada paso
termina en algo revisable, y los tres pasos irreversibles —mergear, migrar Neon,
encender compuertas— piden confirmación explícita antes de ejecutarse.

**Qué NO asume este documento.** No afirma qué está desplegado hoy. Las
compuertas vacías (`ASYNC_IMPORT_ROLLOUT_TENANT_IDS`,
`PURCHASE_COST_ROLLOUT_TENANT_IDS`) sólo dicen que dos capacidades están
apagadas; **la mayoría de las correcciones de este programa son
incondicionales** y su presencia en producción depende de la versión desplegada,
no de una flag. Por eso el paso 0 es establecer esa versión, no deducirla.

---

## Paso 0 — Establecer el estado real de producción

Sin esto, todo lo demás se apoya en una suposición.

| Dato | Cómo se obtiene | Por qué importa |
|---|---|---|
| Versión desplegada en `vektor-api` y `vektor-worker` | Railway → cada servicio → último deploy (commit SHA) | Dice qué correcciones incondicionales YA están activas y cuáles no |
| `alembic_version` efectivo en Neon | `scripts/migrate_preflight.py` en el log del último deploy, o `SELECT * FROM alembic_version` con la `DATABASE_URL` del shell | Dice cuáles de las 7 migraciones faltan de verdad |
| Configuración efectiva por servicio | Railway → Variables, en **api y worker por separado** | Los dos son servicios distintos con entorno propio y Railway los redespliega en paralelo (ver el snapshot de capacidades de E7a-lite) |

Los tres son lecturas. Ninguno cambia nada.

> **Advertencia de ámbito.** `vektor-api` y `vektor-worker` se redespliegan en
> paralelo sin orden garantizado. Una compuerta encendida en uno y apagada en el
> otro es un estado alcanzable, y es exactamente lo que `capabilities_json`
> (mig `20260910_0005`) detecta y rechaza en vez de escribir bajo reglas
> distintas de las que el usuario vio.

---

## Paso 1 — Dejar la PR evaluable *(hecho)*

La PR #60 estaba **CONFLICTING**, y GitHub no corre `pull_request` sobre una PR
en ese estado: no había CI, ni error visible que lo explicara.

Faltaba un solo commit de `main` (`a12f018a`, PR #56), con dos conflictos:

- `ingestion_import_service.py`: **un** hunk. `main` traía
  `_load_import_fingerprints` (todas las huellas del tenant); la rama trae la
  precarga acotada de E6c-1, que lo reemplazó con medición (32,3 MB → 0,8 MB).
  Resuelto a favor de la rama. El resto de #56 entró por auto-merge.
- `test_ingestion_statement_budget_pg.py`: add/add, 7 hunks de contexto. Se
  verificó que la versión de la rama es **superconjunto estricto** —ninguna
  función ni presupuesto de `main` se pierde— y se tomó entera.

Verificado después del merge: `ruff`, `mypy` (848 archivos), la compuerta de
statements (8 tests) y la suite completa.

---

## Paso 2 — CI verde sobre la PR

Con la PR ya mergeable, GitHub corre `ci-backend` y `ci-frontend`.

**El CI es una compuerta necesaria, no la única.** Que esté verde dice que el
código compila, tipa y pasa los tests que alguien escribió; no dice que el cambio
sea correcto, ni que las migraciones se apliquen bien sobre datos reales, ni que
el entorno que las va a recibir esté en condiciones. Las cuatro compuertas de
esta entrega son:

1. **CI verde** — `ci-backend` (ruff + mypy + pytest con cobertura ≥ 60 % + build
   Docker) y `ci-frontend` (tsc + ESLint + `next build` + jest).
2. **Revisión del diff** — 65 commits. Lo que más pide lectura humana: las
   decisiones de identidad (`domain/operation_identity.py`), el motor de costos
   (F-H6.c/d) y el recorrido asíncrono, porque son los que cambian QUÉ se
   persiste y no sólo cómo.
3. **Prueba de las migraciones** — sobre una copia con datos, no sólo sobre la
   base vacía del CI. Ver paso 3.
4. **Verificación operativa previa al piloto** — paso 4.

Sobre la corrida local: no reemplaza al CI y puede mentir en las dos direcciones.
El entorno local hereda `.env`, que apunta a Neon con `sslmode=require`, y eso
hizo fallar 4 tests de `test_ingestion_retry_pg.py` que en CI pasan — fallas del
entorno, no del código.

---

## Paso 3 — Migraciones (revisable antes de aplicar)

Siete migraciones nuevas respecto de `main`, en cadena, con cabeza única
`20260910_0005`:

| Migración | Qué hace | Reversible |
|---|---|---|
| `20260906_0001_uploaded_file_parse_attempt` | +1 columna | sí |
| `20260909_0001_job_runs` | +1 tabla, +1 índice | sí |
| `20260910_0001_operation_identities` | +2 tablas, +1 índice | sí |
| `20260910_0002_external_code` | +3 columnas, +1 índice único parcial | sí |
| `20260910_0003_master_user_edits` | +1 columna | sí |
| `20260910_0004_import_attempts_outbox` | +2 tablas, +3 índices | sí |
| `20260910_0005_import_attempt_capabilities` | +1 columna (`capabilities_json`) | sí |

**Las siete son aditivas en el sentido que importa para desplegar sin ventana**:
ninguna borra ni altera datos existentes, así que la versión vieja del código
convive con las columnas y tablas nuevas sin verlas (ver `project_forward_deploy`).

**Aditiva NO quiere decir reversible sin pérdida.** La columna `downgrade` de la
tabla dice que el downgrade existe y es simétrico *en el esquema* — no que sea
inocuo. Un `downgrade` **borra la información que se escribió en lo nuevo**: las
identidades de operación reclamadas, los códigos externos de maestros, los
intentos de importación y sus capacidades congeladas, la traza de `job_runs`.
Bajar el esquema después de haber importado con él significa perder la evidencia
de qué se importó y con qué reglas, y las próximas importaciones dejan de poder
reconocer como repetido lo que ya estaba cargado.

Por eso las tres reversas son cosas distintas y no se pueden pensar juntas — ver
la sección **Reversa** al final.

**El único riesgo teórico está descartado por construcción.** El índice único
parcial de `20260910_0002` se crea sobre `(tenant_id, external_code_key)` con
predicado `external_code_key IS NOT NULL`, y esa columna **nace en la misma
migración**: en el momento de crearlo el conjunto indexado está vacío, así que no
puede chocar con datos existentes.

Se aplican solas en el pre-deploy de Railway (`preDeployCommand`), *después* del
merge. Si fallan, `set -e` aborta el deploy y la versión vieja sigue sirviendo.

**Probarlas antes sobre datos, no sólo sobre la base vacía del CI**: una rama de
Neon o un dump restaurado, corriendo `alembic upgrade head` y midiendo cuánto
tarda. Es el único momento en que un `CREATE INDEX` sobre una tabla grande se
puede descubrir sin que sea en producción.

**`20260910_0005` es la que faltaba en la checklist anterior** y es la que el
worker escribe: sin ella, el ejecutor asíncrono falla contra una columna
inexistente. No encender la compuerta asíncrona antes de confirmarla aplicada.

---

## Paso 4 — Dependencias del piloto

Qué tiene que ser cierto **antes** de encender nada, y cómo se verifica:

1. `alembic_version` en Neon = `20260910_0005` (paso 0, repetido después del deploy).
2. `vektor-api` y `vektor-worker` en el **mismo** commit. Si divergen, esperar.
3. **Cola `ingestion` consumiendo** — estado: **NO VERIFICADO**. Lo que se sabe
   es que `CELERY_QUEUES` puede estar sobrescrita en el worker y que desde el
   repositorio no se puede saber si lo está. No es lo mismo que "no funciona".
   Evidencia que lo cierra: el valor efectivo de `CELERY_QUEUES` en las variables
   de `vektor-worker`, más una task de la cola `ingestion` ejecutada con éxito en
   las últimas 24 h (`scripts/diag_job_runs.py` la lista).
4. **Beat** — estado: **NO VERIFICADO**. `backend/beat/railway.toml` existe en el
   repo como referencia y `CLAUDE.md` afirma que no está desplegado, pero eso es
   una afirmación del repositorio, no una lectura de Railway: exactamente la
   clase de deducción que el paso 0 existe para no hacer. Evidencia que lo
   cierra: la lista de servicios de Railway del proyecto, y `job_runs` mostrando
   (o no) ejecuciones periódicas del barrido de relecturas.

   Si resulta que no está desplegado, es un **límite conocido y no un
   bloqueante**: los guards reactivos de los dos endpoints de relectura cierran
   los runs colgados cuando alguien vuelve a tocar ese tenant. Lo que no hay en
   ese caso es barrido global.

**Regla para los cuatro: se registra la versión, la configuración y la evidencia
—con fecha— antes de habilitar el piloto.** Un ítem sin evidencia queda como NO
VERIFICADO en el registro, nunca como "asumido OK".

---

## Paso 5 — Piloto por tenant

Las dos compuertas se encienden **de a una y por tenant**, no juntas ni globales.

**Las dos compuertas no son la misma clase de llave, y apagarlas no cuesta lo
mismo.** Confundirlas es lo que haría que apagar algo "por las dudas" rompiera
trabajo que estaba bien encaminado.

| Compuerta | Clase | Qué pasa al apagarla |
|---|---|---|
| `ASYNC_IMPORT_ROLLOUT_TENANT_IDS` | **Habilita solicitudes nuevas.** Gatea SÓLO el registro del intento | Deja de aceptar imports nuevos por esa ruta. Los intentos **ya registrados terminan**: el publicador y el recuperador corren siempre, a propósito, porque si no las órdenes ya commiteadas quedarían huérfanas. El frontend cae a `/confirm`, que sigue vigente |
| `PURCHASE_COST_ROLLOUT_TENANT_IDS` | **Cambia las reglas de interpretación.** Decide QUÉ se persiste de una compra | Los intentos en vuelo de ese tenant **se invalidan** con `ERROR_CAPACIDADES`, no reintentable: el snapshot de `capabilities_json` (mig `20260910_0005`) congela las compuertas al confirmar y el ejecutor las verifica antes de escribir, justamente para no importar bajo reglas distintas de las que el usuario vio. Falla con mensaje accionable y **sin escribir nada** |

Consecuencia práctica: la primera se puede apagar en cualquier momento sin
coordinar. La segunda se gira **cuando no haya un import corriendo** para ese
tenant, y cada giro —encender o apagar— tiene ese precio.

Runbooks propios: `docs/runbooks/async_import_rollout.md` y
`docs/runbooks/purchase_cost_rollout.md`.

**Tenant del piloto: a decidir, no elegido acá.** El criterio que propongo, para
que la decisión sea sobre algo:

- Un tenant de prueba antes que uno real. `feedback_test_data_on_demo_tenants`
  ya fijó que los archivos inventados van sobre un tenant demo.
- Que tenga archivos reales ya subidos, para que el piloto ejerza el código y no
  sólo lo encienda.
- Que el dueño sepa que está en piloto y a quién avisarle si algo se ve raro.

---

## Paso 6 — Qué se mira después de encender

No "si anduvo", sino datos concretos:

- `pipeline_events` del tenant piloto: cada confirm deja `STAGE_CONFIRM` con el
  snapshot del mapeo, y cada rechazo previo al lease deja `STAGE_REJECT` con
  motivo. Un confirm sin ninguna de las dos filas es la señal de alarma.
- `import_attempts`: intentos en `FALLADO` y su `error_code`.
- `unclassified_records` del tenant: si el volumen de «Otros» sube de golpe, la
  interpretación empeoró aunque nada haya fallado.
- Sentry, filtrando por `service` (api / worker / mcp).

## Cómo se verifica que el código nuevo está DESPLEGADO

Esta sección existe porque la primera versión del runbook decía "mirar Railway",
y eso **no alcanza**: cuando un deploy falla, Railway lo marca Failed y deja el
anterior **Active y en verde**. "Todo verde y corriendo" es exactamente el mismo
cuadro para «se desplegó bien» y para «no se desplegó y quedó el viejo».

Lo que NO sirve, comprobado el 2026-09-12:

- **El ID de deployment de Railway** no es el commit. Son UUIDs internos.
- **`/health`** devuelve `version: "1.0.0"` **hardcodeada** (`app/main.py`). No
  dice nada del commit.
- **Que el esquema esté al día** tampoco: las migraciones las aplica un
  contenedor one-off, y alguien pudo correrlas a mano desde su shell.
- **`job_runs` vacía** no prueba nada: la escribe SÓLO el barrido de relecturas,
  que agenda Beat — y Beat no está desplegado. Cero filas se explica entero por
  eso.

Lo que sí sirve: **una sonda de ruta**. Se elige un endpoint que exista sólo en
el código nuevo y que pida autenticación, y se lo llama SIN token desde el
navegador:

```
https://<api>/health                                     ← control: debe dar 200
https://<api>/api/v1/ingestion/imports/00000000-0000-0000-0000-000000000000
```

- `{"detail":"Not authenticated"}` → **la ruta existe** → el código nuevo está
  desplegado. (Que rechace por credenciales ya prueba que la ruta está.)
- `{"detail":"Not Found"}` → la ruta no existe → corre código viejo.

El control (`/health`) evita el falso negativo más obvio: un 404 porque la URL
estaba mal. La URL pública sale de Railway → `vektor-api` → Settings →
Networking, o de `NEXT_PUBLIC_API_URL` en Vercel.

Para el próximo deploy hay que elegir una sonda nueva: la de arriba ya está en
`main` y a partir de ahora responde igual con código viejo y nuevo.

## Deriva entre esquema y `alembic_version`

`scripts/diag_schema_drift.py` (read-only, lee `DATABASE_URL` del `.env` solo —
sin argumentos, sin comillas, sin `$()`):

```
cd backend && .venv/bin/python scripts/diag_schema_drift.py
```

Compara cada objeto contra la versión declarada y clasifica en cuatro estados. El
que importa es **DERIVA** (el objeto existe pero su migración no está aplicada):
es lo que hace fallar el `preDeployCommand` con `DuplicateColumn`, y el mensaje
de alembic no alcanza para saber si es una columna suelta o media cadena.

## Reversa — tres cosas distintas, en este orden

No son escalones de lo mismo. Cada una revierte algo diferente, cuesta distinto y
casi siempre **alcanza con la primera**.

### 1. Desactivar una capacidad (flag)

Sacar el tenant de la lista. Inmediato, sin deploy, **sin pérdida de datos**. Lo
ya importado queda como está. Es la reversa correcta para "esto no está saliendo
como esperaba con este tenant".

Coste según la clase de compuerta: apagar la entrada asíncrona no interrumpe los
intentos registrados; apagar el motor de costos invalida los que estén en vuelo
(ver paso 5).

### 2. Revertir la aplicación (código)

Redeploy de la versión anterior en Railway. **Sin pérdida de datos**, porque el
esquema es aditivo y la versión vieja simplemente no mira lo nuevo. Es la reversa
para un defecto en el código, no en los datos.

Cuidado con la asimetría: `vektor-api` y `vektor-worker` se redespliegan por
separado. Revertir uno solo deja un estado mixto — que el snapshot de
capacidades detecta para el motor de costos, pero que conviene no producir a
propósito.

### 3. Revertir el esquema (migraciones) — último recurso, CON pérdida

`downgrade` existe, es simétrico y está probado, pero **borra la información que
se escribió en lo nuevo**: identidades de operación reclamadas, códigos externos
de maestros, intentos de importación con sus capacidades congeladas, la traza de
`job_runs`.

Lo que se pierde no es sólo historia: las identidades reclamadas son lo que le
permite a la próxima importación reconocer como repetido lo que ya está cargado.
Sin ellas, reimportar el mismo archivo vuelve a duplicar.

Nunca es parte de una reversa de rutina. Requiere: motivo escrito, backup de
Neon tomado antes, y la decisión explícita de que esos datos se pueden perder.
Para casi todo lo demás, alcanza con (1) o (2).

---

## Registro del despliegue — 2026-09-12

Primera entrega ejecutada con este runbook. Lo verificado, con la evidencia:

| Ítem | Estado | Evidencia |
|---|---|---|
| Merge a `main` | ✅ | `e1d1b920`, merge commit con los 66 commits preservados |
| Esquema de Neon | ✅ | `diag_schema_drift.py`: `alembic_version` = `20260910_0005`, 13/13 objetos, **sin deriva ni faltantes** |
| Código nuevo sirviendo | ✅ | Sonda de ruta: `/api/v1/ingestion/imports/{uuid}` devuelve `Not authenticated` (la ruta existe; entró a `main` sólo con este merge, commit `aedb34af`) |
| `vektor-api` / `vektor-worker` | ✅ | Ambos Active |
| Compuertas | Apagadas, como se planeó | `ASYNC_IMPORT_ROLLOUT_TENANT_IDS` y `PURCHASE_COST_ROLLOUT_TENANT_IDS` vacías |
| Cola `ingestion` | **NO VERIFICADO** | Falta el valor efectivo de `CELERY_QUEUES` en `vektor-worker` |
| Beat | **NO VERIFICADO** | `job_runs` vacía es consistente con «no desplegado» y también con «desplegado y sin ejecuciones»: no distingue |

### Lo que salió mal y qué dejó

Un deploy falló con `DuplicateColumn` sobre `uploaded_files.parse_attempt_id`:
`alembic_version` decía `20260903_0001` y la columna ya existía. El fail-safe
funcionó — `set -e` abortó, la versión vieja siguió sirviendo, nada a medias.

Un deploy posterior sí aplicó la cadena completa. **No se estableció cuál de los
dos órdenes ocurrió** (si el log era de un intento anterior al del merge, o si
hubo un reintento): los dos terminan igual en la base, y la distinción se perdió
por no mirar la lista de deploys con sus commits en el momento.

Lo que sí quedó aprendido y ya está arriba: cómo verificar qué código corre, y
que ni el ID de deployment, ni `/health`, ni el estado del esquema lo responden.

### Pendiente que este deploy dejó abierto

Las 7 migraciones **no son idempotentes**. El repo documenta en `20260806_0001`
que «el `preDeployCommand` puede correr dos veces» y usa `sa.inspect(bind)` +
chequeo previo; las 7 nuevas no siguieron esa convención, y por eso un esquema
con deriva las hace fallar en vez de saltear lo que ya está. No urge —la deriva
se resolvió sola— pero es la causa raíz de la caída y vuelve a morder.
