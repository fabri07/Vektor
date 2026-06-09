# Runbook — Google MCP / OAuth (FASE 5)

Guía operativa para diagnosticar y resolver fallos al conectar Google (Gmail,
Calendar, Sheets, Drive, Docs) vía el MCP server. El **código de la integración
está completo**; los fallos son de configuración/entorno.

## Herramienta de diagnóstico

`GET /api/v1/integrations/google/diagnostics` (rol **SUPERADMIN**) corre todos los
chequeos verificables desde el backend y reporta qué falta, sin exponer secretos.

```bash
curl -H "Authorization: Bearer <token-superadmin>" \
  https://<backend>/api/v1/integrations/google/diagnostics
```

Respuesta: `{ overall_ok, checks[], tenant_connection }`. Cada check trae
`{check, ok, severity, detail}`. `severity`: `error` (bloquea), `warning`,
`info` (no verificable desde el backend → hint manual).

## Checks y cómo resolverlos

| check | Qué valida | Si falla |
|-------|-----------|----------|
| `flag_enabled` | `ENABLE_GOOGLE_MCP_TOOLS` | Activar el flag en el backend (`.env` / Railway). Sin esto el backend ignora el MCP por completo. |
| `mcp_url_configured` | `MCP_SERVER_URL` no vacío | Setear la URL del MCP server (ej. `https://vektor-mcp-production.up.railway.app`). |
| `shared_secret_configured` | `MCP_SERVER_SHARED_SECRET` presente | Generar un secreto y configurarlo **idéntico** en backend y MCP server. Vacío → el MCP rechaza con 401. |
| `mcp_server_reachable` | `GET {MCP_SERVER_URL}/health` → 200 | El MCP server no responde: verificar que `vektor-mcp` esté deployado/corriendo y la URL correcta. |
| `mcp_auth` | `/auth/status` con el shared secret | `401` → el secret no coincide entre backend y MCP server. `mcp_unavailable` → server caído. |
| `redirect_uri_hint` | (info) | **No verificable desde el backend.** En Google Cloud Console el Redirect URI registrado debe ser el del **MCP server** (`…/auth/callback`), NO el del backend, y coincidir exactamente con `GOOGLE_MCP_OAUTH_REDIRECT_URI` del MCP server. Mismatch → `token_exchange_failed:redirect_uri_mismatch`. |
| `scopes_hint` | (info) | Los scopes solicitados deben estar habilitados en la pantalla de consentimiento. La app debe estar **verificada** (o el usuario en la lista de testers) para evitar el warning "app no verificada" → que causa abandono/timeout. |

## Errores frecuentes (`last_error_code` del tenant)

- `oauth_callback_timeout` — el usuario no completó el flujo en 10 min (a menudo por
  la pantalla "app no verificada"). Verificar app verification / lista de testers.
- `token_exchange_failed:redirect_uri_mismatch` — Redirect URI en Google Cloud ≠ el
  del MCP server. **Causa más común cuando todo lo demás está OK.**
- `insufficient_scope` — el usuario no concedió todos los scopes, o la app no los pide.
- `refresh_failed` — el refresh token se revocó/expiró. Desconectar y reconectar desde `/apps`.
- `http_401` / `mcp_auth_required` — `MCP_SERVER_SHARED_SECRET` distinto entre backend y MCP.

## Env vars exactas a revisar (Railway)

**Backend (`vektor-api`):**
- `ENABLE_GOOGLE_MCP_TOOLS=true`
- `MCP_SERVER_URL=https://<mcp-server>`
- `MCP_SERVER_SHARED_SECRET=<secreto>`

**MCP server (`vektor-mcp`):**
- `MCP_SERVER_SHARED_SECRET=<secreto>` — **idéntico** al del backend
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
- `GOOGLE_MCP_OAUTH_REDIRECT_URI=https://<mcp-server>/auth/callback`

**Google Cloud Console (Credentials → OAuth client):**
- Authorized redirect URI **exactamente igual** a `GOOGLE_MCP_OAUTH_REDIRECT_URI`
  (mismo esquema, host y path; sin barra final de más).
- Scopes habilitados en la pantalla de consentimiento; app verificada o la cuenta
  de prueba agregada como tester.

## Orden de diagnóstico recomendado

1. Correr `/integrations/google/diagnostics`. Resolver primero los `severity=error`.
2. Si todos los `error` pasan pero la conexión sigue fallando → el problema está en
   los `info` (redirect URI o app verification en Google Cloud), que requieren acceso
   a la consola de Google. Reproducir el flujo desde `/apps` y leer el
   `last_error_code` en `tenant_connection`.
3. Confirmar en Google Cloud Console: Redirect URI = `…/auth/callback` del MCP server;
   scopes habilitados; app verificada o usuario en testers.
