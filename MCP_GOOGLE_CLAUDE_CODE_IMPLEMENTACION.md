# Implementación propuesta: agentes + servicios de Google vía MCP (Claude Code)

## Objetivo
Eliminar integraciones directas OAuth/SDK de Google en backend y delegar acceso a Gmail/Calendar/Drive/Sheets a herramientas MCP invocadas por agentes desde Claude Code.

## Principios de arquitectura
1. **Backend sin tokens Google**: no guardar `access_token` ni `refresh_token` en la base de datos del producto.
2. **Tool-calling por MCP**: los agentes solo emiten intención + payload estructurado.
3. **Boundary claro**:
   - `Agent*` decide *qué* hacer.
   - `McpToolGateway` ejecuta *cómo* hacerlo contra herramientas MCP.
4. **Aprobación humana** para acciones con efectos externos (enviar mail, crear evento, subir archivo).
5. **Auditabilidad completa**: cada llamada MCP deja huella (tool, args hash, resultado, latencia, actor).

## Diseño de componentes

### 1) Capa de aplicación
Crear interfaz única:

- `app/application/ports/mcp_gateway.py`
  - `list_tools()`
  - `call_tool(tool_name: str, args: dict) -> dict`
  - `health() -> dict`

Implementación concreta:

- `app/integrations/mcp/claude_code_gateway.py`
  - Cliente hacia runtime MCP (stdio/http según despliegue)
  - Retry con backoff y timeout por herramienta
  - Sanitización de errores y redacción de secretos en logs

### 2) Adaptador por dominio Google
Crear wrappers semánticos (sin SDK Google):

- `GoogleMcpService.read_inbox(...)`
- `GoogleMcpService.create_draft(...)`
- `GoogleMcpService.create_calendar_event(...)`
- `GoogleMcpService.append_sheet_rows(...)`
- `GoogleMcpService.upload_drive_file(...)`

Cada wrapper traduce contrato de negocio -> contrato de herramienta MCP.

### 3) Agentes
Actualizar agentes para usar `GoogleMcpService` y no OAuth directo.

- `AgentSupplier`: clasifica correo y propone borrador (pending action). Al confirmar, llama tool MCP `google.gmail.create_draft`.
- `AgentCalendar`: prepara evento y, tras aprobación, llama `google.calendar.create_event`.
- `AgentCash`: importación de hojas vía `google.sheets.read_range` + validación local.

### 4) Pending actions
Mantener patrón de dos fases:
1. `chat` crea acción pendiente con payload canónico.
2. `confirm` ejecuta llamada MCP y registra resultado.

Recomendado en payload:
- `tool_name`
- `tool_args`
- `idempotency_key`
- `expected_effect`

## Seguridad

## Autorización
- Allowlist de herramientas por agente (ejemplo: Supplier solo Gmail tools).
- Validación estricta de argumentos con Pydantic antes de invocar MCP.

## Idempotencia
- Hash por (`tenant_id`, `tool_name`, `normalized_args`) para evitar ejecuciones duplicadas.

## Observabilidad
Registrar en audit log:
- `mcp_server`, `tool_name`, `duration_ms`, `status`, `error_code`.
- Nunca loguear contenido sensible completo; usar truncado/hash.

## Timeouts y circuit breaker
- Timeout por tool (ej: 8s lectura, 20s escritura).
- Circuit breaker por tool para evitar cascada de fallas.

## Plan de migración (paso a paso)
1. **Eliminar integraciones Workspace directas** (API/routes/modelos/servicios).
2. **Agregar `McpToolGateway` + `GoogleMcpService`** con contratos internos.
3. **Migrar agentes** a wrappers MCP.
4. **Ajustar `pending_action_service`** para ejecuciones externas vía tool-calling.
5. **Agregar pruebas** con mocks de MCP:
   - éxito
   - timeout
   - tool inexistente
   - error transitorio y retry
6. **Feature flag**: `ENABLE_GOOGLE_MCP_TOOLS=true` para rollout gradual.
7. **Runbook operativo**: errores frecuentes y recuperación.

## Contratos sugeridos de herramientas MCP
- `google.gmail.list_messages`
- `google.gmail.get_message`
- `google.gmail.create_draft`
- `google.calendar.list_events`
- `google.calendar.create_event`
- `google.sheets.read_range`
- `google.sheets.append_rows`
- `google.drive.upload_file`

## Ejemplo de ejecución en confirmación
1. Usuario confirma acción.
2. Backend valida esquema del payload.
3. Backend llama `McpToolGateway.call_tool(tool_name, args)`.
4. Si OK: `execution_status=SUCCEEDED`.
5. Si timeout/transitorio: `FAILED_RETRYABLE`.
6. Si error permanente (argumento/scope): `FAILED_NON_RETRYABLE`.

## Checklist de Done
- [ ] No hay módulos OAuth Workspace ni SDK Google en backend.
- [ ] Todas las operaciones Google pasan por `McpToolGateway`.
- [ ] Pending actions externas quedan auditadas con metadata MCP.
- [ ] Tests de contrato MCP (unit + integración mock) en verde.
- [ ] Runbook y alertas de observabilidad publicados.
