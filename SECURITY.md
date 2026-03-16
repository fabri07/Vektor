# Véktor — Security Checklist

> Última actualización: 2026-03-16
> Alcance: backend FastAPI (Python 3.12) — v1

---

## 1. Validación de Inputs

| Ítem | Estado | Detalle |
|------|--------|---------|
| Todos los endpoints usan schemas Pydantic | ✅ Implementado | Todos los routers declaran modelos de request/response |
| Montos: `gt=0`, `le=999_999_999` | ✅ Implementado | `transaction.py` — `_MAX_AMOUNT = Decimal("999999999")` |
| Fechas: no-futuro en ventas y gastos | ✅ Implementado | `@field_validator` en `CreateSaleRequest`, `CreateExpenseRequest` y sus variantes Update/Bulk |
| Strings: `max_length` en todos los campos TEXT | ✅ Implementado | `notes≤1000`, `description≤500`, `supplier_name≤300`, `name≤300`, etc. |
| Notificaciones: `max_length` en title/body | ✅ Implementado | `title≤200`, `body≤2000`, `notification_type` pattern `^[a-zA-Z0-9_]{1,50}$` |
| Archivos: re-validar MIME server-side | ✅ Implementado | `filetype.guess()` (magic bytes) en `ingestion.py` y `files.py` |
| Archivos: sanitización de nombre | ✅ Implementado | `_sanitize_filename()` en `ingestion.py` y `files.py` — elimina path traversal y chars peligrosos |
| Archivos: rechazo > 10 MB | ✅ Implementado | `MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024` |
| Presigned URLs (no S3 keys expuestos) | ✅ Implementado | `/files/{id}/url` retorna URL pre-firmada, nunca la clave S3 |

---

## 2. Tenant Isolation

| Ítem | Estado | Detalle |
|------|--------|---------|
| `tenant_id` en WHERE de TODOS los repositories | ✅ Implementado | Auditado: `BaseRepository`, `SaleRepository`, `ExpenseRepository`, `ProductRepository`, `UserRepository`, `HealthScoreRepository`, `FileRepository`, `TenantRepository` |
| `get_current_tenant` inyecta tenant desde JWT | ✅ Implementado | `deps.py` — extrae `tenant_id` del token, fuerza aislamiento en todo write |
| `UserRepository.get_by_email_any_tenant()` | ✅ Mitigado | Único método sin filtro de tenant — usado solo en login, tenant verificado vía JWT después |
| Test de aislamiento (S5) | ✅ Passing | `test_s5_tenant_isolation` — verifica 0 data leaks entre tenants |

---

## 3. Rate Limiting

| Ítem | Estado | Detalle |
|------|--------|---------|
| `slowapi` instalado | ✅ Implementado | `requirements.txt` — `slowapi==0.1.9` |
| Límite global | ✅ Implementado | `200/minute` por IP (default en `limiter`) |
| `/auth/register` | ✅ Implementado | `5/10minutes` por IP |
| `/auth/login` | ✅ Implementado | `10/5minutes` por IP |
| `/ingestion/upload` | ✅ Implementado | `20/hour` por IP |
| Rate limit en `/files/upload` | ⏳ Pendiente | Agregar `@limiter.limit` en `files.py` upload si se expone públicamente |

---

## 4. File Upload Security

| Ítem | Estado | Detalle |
|------|--------|---------|
| Validación MIME por magic bytes (python filetype) | ✅ Implementado | `filetype.guess()` en ambos endpoints de upload |
| Rechazo de archivos > 10 MB | ✅ Implementado | Ambos endpoints (`files.py`, `ingestion.py`) |
| Sanitización de nombre de archivo | ✅ Implementado | `_sanitize_filename()` — strip path traversal + regex `[^a-zA-Z0-9.\-_]` |
| S3 keys no expuestos al cliente | ✅ Implementado | Solo presigned URLs |
| Antivirus / ClamAV scan | ⏳ Pendiente | Escaneo de malware antes de persistir en S3 |
| Retención de archivos (borrado automático) | ⏳ Pendiente | Política de expiración para archivos > 90 días |

---

## 5. Security Headers (Middleware FastAPI)

| Header | Estado | Valor configurado |
|--------|--------|-------------------|
| `X-Content-Type-Options` | ✅ Implementado | `nosniff` |
| `X-Frame-Options` | ✅ Implementado | `DENY` |
| `X-XSS-Protection` | ✅ Implementado | `1; mode=block` |
| `Referrer-Policy` | ✅ Implementado | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | ✅ Implementado | `default-src 'none'; frame-ancestors 'none'` |
| `Strict-Transport-Security` | ✅ Implementado | `max-age=31536000; includeSubDomains` (solo `ENVIRONMENT=production`) |
| CORS `allow_methods` | ✅ Corregido | `["GET","POST","PATCH","DELETE","OPTIONS"]` (antes: `["*"]`) |
| CORS `allow_headers` | ✅ Corregido | `["Authorization","Content-Type","Accept","X-Request-ID"]` (antes: `["*"]`) |

---

## 6. Logging Estructurado

| Ítem | Estado | Detalle |
|------|--------|---------|
| Formato JSON en producción | ✅ Implementado | `structlog.processors.JSONRenderer()` cuando `is_production` |
| Log de requests HTTP | ✅ Implementado | Middleware `request_logger` — `method`, `path`, `status_code`, `duration_ms` |
| Passwords NO loggeados | ✅ Implementado | Ningún endpoint loguea body completo; exception handler loguea solo `exc_type`/`exc_msg` |
| Tokens NO loggeados | ✅ Implementado | `auth_service.py` y `security.py` no loguean tokens |
| Datos financieros completos NO loggeados | ✅ Implementado | Solo `tenant_id`, `score_total`, `confidence_level` en jobs |
| Log de intentos de login | ⏳ Pendiente | Agregar `logger.warning("auth.login_failed", ...)` en `AuthService.login()` |
| Correlation ID / Request ID | ⏳ Pendiente | Middleware para propagar `X-Request-ID` en todos los logs |
| Audit trail de modificaciones de datos | ⏳ Pendiente | `decision_audit_log` cubre scores; crear equivalente para ventas/gastos |

---

## 7. Variables de Entorno y Secrets

| Ítem | Estado | Detalle |
|------|--------|---------|
| `.env.example` documenta todas las variables | ✅ Implementado | Incluye `OCR_BACKEND`, hint de generación de JWT_SECRET |
| Ningún secret hardcodeado en código | ✅ Implementado | Defaults inseguros explícitamente etiquetados como `insecure-*` |
| `JWT_SECRET_KEY` ≥ 32 chars en producción | ✅ Implementado | `model_validator` en `Settings` — lanza `ValueError` si falla en prod |
| `SECRET_KEY` ≥ 32 chars en producción | ✅ Implementado | Mismo validator |
| `.env` en `.gitignore` | ✅ Implementado | Verificado en `.gitignore` |
| Secrets en CI/CD (GitHub Actions) | ⏳ Pendiente | Configurar `secrets.*` en workflows de CI |
| Redis con AUTH (password) | ⏳ Pendiente | Agregar `REDIS_PASSWORD` a settings y URL |
| Redis con TLS en producción | ⏳ Pendiente | Cambiar `redis://` → `rediss://` en producción |

---

## 8. Autenticación y Autorización

| Ítem | Estado | Detalle |
|------|--------|---------|
| JWT HS256 con expiración | ✅ Implementado | Access: 30 min (configurable), Refresh: 7 días |
| Bcrypt hashing (12 rounds) | ✅ Implementado | `passlib[bcrypt]` con `rounds=12` |
| RBAC: OWNER / ADMIN / ANALYST / VIEWER | ✅ Implementado | `require_role()` dependency en endpoints sensibles |
| Docs (/docs, /redoc) ocultos en producción | ✅ Implementado | `docs_url=None` si `is_production` |
| Account lockout tras N intentos fallidos | ⏳ Pendiente | Implementar contador en Redis + bloqueo temporal |
| Refresh token blacklist | ⏳ Pendiente | Persistir tokens revocados en Redis al hacer logout |
| 2FA / MFA | ⏳ Pendiente | TOTP (Google Authenticator) para cuentas OWNER |

---

## Resumen Ejecutivo

| Categoría | Ítems | ✅ Implementado | ⏳ Pendiente |
|-----------|-------|-----------------|--------------|
| Validación de inputs | 9 | 9 | 0 |
| Tenant isolation | 4 | 4 | 0 |
| Rate limiting | 6 | 5 | 1 |
| File upload | 6 | 5 | 1 |
| Security headers | 8 | 8 | 0 |
| Logging | 8 | 5 | 3 |
| Variables / Secrets | 8 | 5 | 3 |
| Auth / Authz | 7 | 5 | 2 |
| **TOTAL** | **56** | **46 (82%)** | **10 (18%)** |

### Ítem crítico pendiente antes de producción
1. `JWT_SECRET_KEY` y `SECRET_KEY` deben ser secretos reales ≥ 32 chars (ya validado en Settings — solo falta configurar en CI/CD)
2. Redis con AUTH password en producción
3. Account lockout en login (anti-brute-force complementario al rate limiting)

