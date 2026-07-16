# Stitch MCP Proxy for Véktor

Este directorio deja listo un proxy `stdio` local para conectar Google Stitch con Codex usando MCP.

## Qué hace

- Usa el SDK oficial `@google/stitch-sdk`
- Expone Stitch como un servidor MCP `stdio`
- Permite que Codex lo consuma como herramienta MCP local
- Mantiene las credenciales fuera del repo si usás `.env`

## Archivos

- `package.json` — dependencias del proxy
- `proxy.mjs` — arranque del `StitchProxy`
- `run.sh` — carga `backend/.env` como fallback, luego `.env` local, y lanza el proxy
- `register-codex.sh` — registra el proxy en Codex
- `.env.example` — variables necesarias

## Variables de entorno

Autenticación oficial soportada por el SDK de Stitch:

### Opción A: API key

```env
STITCH_API_KEY=tu_api_key
```

### Opción B: OAuth

```env
STITCH_ACCESS_TOKEN=tu_access_token
GOOGLE_CLOUD_PROJECT=tu_google_cloud_project
```

### Opcionales

```env
STITCH_HOST=https://stitch.googleapis.com/mcp
STITCH_TIMEOUT_MS=300000
```

## Instalación local

Requisitos:

- Node.js 20 o superior
- npm

Instalación:

```bash
cd /Users/fabriziosola/dev/vektor/Vektor/tools/stitch-proxy
npm install
cp .env.example .env
```

Después completá `.env` con `STITCH_API_KEY` o con `STITCH_ACCESS_TOKEN` + `GOOGLE_CLOUD_PROJECT`.

Si ya guardaste `STITCH_API_KEY` en `backend/.env`, `run.sh` la va a leer automáticamente como fallback. Si además definís `tools/stitch-proxy/.env`, ese archivo tiene prioridad y sobrescribe los valores heredados de `backend/.env`.

## Conectar en Codex

### Método recomendado

Este método no guarda secretos en `~/.codex/config.toml` si los dejás en `.env` o en `backend/.env`.

```bash
/Users/fabriziosola/dev/vektor/Vektor/tools/stitch-proxy/register-codex.sh
```

Eso ejecuta internamente:

```bash
codex mcp add stitch -- /Users/fabriziosola/dev/vektor/Vektor/tools/stitch-proxy/run.sh
```

Verificación:

```bash
codex mcp list
codex mcp get stitch
```

Para quitarlo:

```bash
codex mcp remove stitch
```

### Método manual en `~/.codex/config.toml`

Formato real de Codex para un servidor `stdio`:

```toml
[mcp_servers.stitch]
command = "/Users/fabriziosola/dev/vektor/Vektor/tools/stitch-proxy/run.sh"
```

Si preferís guardar credenciales en la config de Codex:

```toml
[mcp_servers.stitch]
command = "/Users/fabriziosola/dev/vektor/Vektor/tools/stitch-proxy/run.sh"

[mcp_servers.stitch.env]
STITCH_API_KEY = "tu_api_key"
```

O con OAuth:

```toml
[mcp_servers.stitch]
command = "/Users/fabriziosola/dev/vektor/Vektor/tools/stitch-proxy/run.sh"

[mcp_servers.stitch.env]
STITCH_ACCESS_TOKEN = "tu_access_token"
GOOGLE_CLOUD_PROJECT = "tu_google_cloud_project"
```

## Cómo lo usaríamos en Véktor

Una vez conectado, podemos pedirle a Codex que use Stitch para:

- proponer variantes visuales de `/apps`
- generar una pantalla más fuerte para onboarding y auth
- extraer HTML/CSS de pantallas de Stitch y convertirlas a React
- usar un mismo lenguaje visual para chat, dashboard y conexiones

## Notas

- Este proxy no fue probado acá con ejecución real porque en este entorno no hay `node` ni `npm` en `PATH`.
- El comando de registro en Codex sí fue verificado localmente y Codex usa el bloque `[mcp_servers.<name>]` en `~/.codex/config.toml`.
- No commitees `.env`.

## Referencias

- Stitch SDK oficial: `@google/stitch-sdk`
- Endpoint MCP oficial de Stitch: `https://stitch.googleapis.com/mcp`
- Codex MCP CLI:

```bash
codex mcp add stitch -- /abs/path/to/run.sh
codex mcp list
codex mcp get stitch
```
