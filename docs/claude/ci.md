# CI — GitHub Actions

Tres workflows: `ci-backend.yml` (ruff + mypy + pytest con cov ≥ 60% + Alembic sobre Postgres + tests `-m postgres` + Docker build), `ci-frontend.yml` (jest + tsc + ESLint + `next build`) y `ci-mcp.yml`. Los tres disparan en push y PR contra `main`/`develop`, filtrados por paths.

## Cupo de minutos

El repo es privado y la cuenta es Free: 2.000 minutos/mes de runners de GitHub. Un run de backend dura ~30 min (23 de pytest), así que el cupo da para ~66 runs. El 2026-10-09 se agotó y sin medio de pago GitHub no levanta los jobs: terminan en `failure` en 3-4 s con la anotación *"The job was not started because recent account payments have failed or your spending limit needs to be increased"*. **Ese `failure` no dice nada del código**: no corrió nada.

- **`concurrency`** en los tres workflows: un push nuevo a un PR cancela el run anterior de la misma rama. En `push` (main) no se cancela nada: cada commit de main tiene que quedar verificado, porque main despliega solo.

## Runner propio (self-hosted)

La variable de repo **`CI_RUNNER`** elige dónde corren los tres workflows:

| `CI_RUNNER` | Dónde corre | Minutos |
|---|---|---|
| vacía / sin definir | `ubuntu-latest` de GitHub | consume cupo |
| `self-hosted` | el runner registrado en la Mac del dev | gratis |

```bash
gh variable set CI_RUNNER --body self-hosted   # pasar al runner propio
gh variable delete CI_RUNNER                   # volver a GitHub
```

Qué cambia en el runner propio (todo en `ci-backend.yml`):

- Los `services:` sólo existen en runners Linux. Con `CI_RUNNER=self-hosted` la imagen del service `postgres` evalúa a `''` y GitHub no lo crea; el paso **"Postgres descartable"** levanta el mismo `postgres:16` con `docker run` y el paso final lo borra (`if: always()`).
- Postgres va en el puerto **55432** en los dos entornos, nunca en 5432: en la Mac el 5432 es el compose local, y los tests PG hacen downgrade/upgrade de migraciones reales.
- **`setup-python` no corre en el runner propio**: el Python que baja para macOS trae fija la ruta `/Users/runner/hostedtoolcache` y falla con `mkdir: /Users/runner: Permission denied`. Se usa el `python3.12` de la máquina (PATH o `~/.local/bin`).
- **"Aislar entorno Python"**: venv por job en `$RUNNER_TEMP`, creado con ese intérprete, para que backend y mcp no compartan dependencias.
- **Sin `actions/cache` en el runner propio** (pip y `node_modules`): los archivos ya quedan en la máquina, y subir `node_modules` a GitHub desde la conexión del dev tardó 5 min.
- **Sin Redis en los tests, en los dos entornos**: `REDIS_URL`, `CELERY_BROKER_URL` y `CELERY_RESULT_BACKEND` apuntan a `localhost:1` (cerrado). La suite asume que Redis no existe (camino de fallback sincrónico); en la Mac el 6379 es el Redis del compose y con él vivo `test_el_fallback_sincronico_deja_el_archivo_rechazado_no_fallado` falla.
- El Docker build requiere Docker Desktop abierto. En la Mac construye para la arquitectura local: verifica el Dockerfile, no la imagen de Railway.

Requisitos: `python3.12` instalado, la Mac prendida con el runner corriendo (si no, los jobs quedan en cola) y Docker Desktop abierto.

**Arrancar el runner desde una terminal limpia.** Los jobs heredan el entorno del proceso del runner: si el shell exportó el `DATABASE_URL` de Neon, cualquier paso que no fije el suyo lo vería. Los pasos de pytest y Alembic fijan el suyo, pero no hay que depender de eso.

### Registrar el runner (una sola vez)

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o runner.tar.gz -L https://github.com/actions/runner/releases/download/v2.338.0/actions-runner-osx-x64-2.338.0.tar.gz
tar xzf runner.tar.gz
TOKEN=$(gh api -X POST repos/fabri07/Vektor/actions/runners/registration-token --jq .token)
./config.sh --url https://github.com/fabri07/Vektor --token "$TOKEN" --name mac-dev --labels self-hosted --unattended
./run.sh            # o ./svc.sh install && ./svc.sh start para que arranque solo
```

En una Mac Apple Silicon, el archivo es `actions-runner-osx-arm64-*`.
