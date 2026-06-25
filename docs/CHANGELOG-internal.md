# Changelog interno — Véktor

> Historia de sprints, proyectos mergeados a `main` y cadena de migraciones Alembic.
> Movido fuera de `CLAUDE.md` (2026-06-25) para abaratar el costo de contexto que se carga en cada sesión.
> `CLAUDE.md` (raíz del repo) mantiene la arquitectura, comandos e invariantes vigentes.

---

## Historial de sprints

| Sprint | Estado | Descripción |
|--------|--------|-------------|
| 1–4 | ✅ | Auth social Google, OAuth frontend, primeras integraciones, Pending Actions externas |
| 5 | ✅ | Chat como home, OAuth federado, adjuntos, analytics Ventas/Gastos/Productos |
| 6 | ✅ | MCP Google: AgentCalendar + AgentSync, BusinessMemory, AgentMemory, file context |
| 7 | ✅ | Data moat: heurísticas JSON, alertas accionables, SmartTable+CSV, forecast 3-tiers, analytics_events |
| 8 | ✅ | Token tracking: LLMCall/UsageSummary, decision_audit_log tokens_*, dashboard cablea endpoints reales |
| 9 | ✅ | Hardening MCP, RBAC sales/expenses/agent, narrativa LLM en insights, cash-breakdown, margin-history |
| 10 | ✅ | Observabilidad, DeterministicFinance, ValidationGate, configuración segura, motor financiero robusto |
| 11 | ✅ | ETL Hardening: provenance tagging, ChatMemoryService, chat unificado con memoria persistente, agent automation rules |
| 12 | ✅ | Custom fields por vertical: `vertical_field_definitions`, `tenant_custom_field_definitions`, undo log, panel en `/settings` + ERD |
| 13 | ✅ | Datos editables/borrables unificados: soft delete auditado en ventas/gastos, sistema de reparación de importaciones mal clasificadas, product lookup en AgentCash |
| 14 | ✅ | Estados canónicos de producto (in_stock/low_stock/out_of_stock/incoming stub), UPDATE_PRODUCT vía chat, Véktor como fuente de verdad, política null/NaN con imputación por mediana, ingesta interactiva con columnas riesgosas |
| 15 | ✅ | **Agent Teams (Stages 1–5d completos):** AgentIncome + AgentExpense (split de AgentCash); AgentGoogle absorbe AgentCalendar + AgentSync; 17 intents rioplatense; AgentTeamPlan/AgentTask; CEO → haiku-4-5; `team_plan_builder.py`. **Stage 3:** `TeamPlanExecutor` DAG (skip downstream en fallo, context N→N+1), `synthesis.py` (sonnet-4-5), planes compuestos, `/confirm/group`. **Stage 4:** `GoogleToolBroker`, 3 ActionTypes Google (`UPLOAD_TO_DRIVE`, `CREATE_GOOGLE_DOC`, `APPEND_TO_SHEET`). **Stage 5d:** AgentCalendar + AgentSync eliminados del registry. |
| 16 | ✅ | **Stage 5a:** AgentHealth v2 — fórmula 5 dims, `sub_collector/calculator/narrator`, `scorer.py` shim, `health_config_service`, PDF/DOCX export, `score_growth` en API/schema, margen configurable en `/settings`. **Stage 5b:** AgentHelper con `docs/vektor_user_manual.yaml` + `help_documentation_service.py`, endpoint `/agent/help/chat` (sin rate limit), `redirect_to` bidireccional, frontend `/help` + `HelpChat`. **Stage 5c:** retry en `TeamPlanExecutor` para tasks externas (1 retry + backoff), structlog `agent_name` en CEO, `test_health_engine_regression.py`, `test_team_plan_executor_retry.py`, `test_help_documentation.py`. PyYAML agregado a deps. |
| 17 | ✅ | **Intents analíticos + tolerancia al lenguaje natural:** catálogo 18→60 intents en 8 familias (archivos, precios, stock, ventas, gastos, proveedores, caja, clientes-stub, fallback). 7 ActionTypes analíticos read-only (`ANALYZE_*`, `SIMULATE_SCENARIO`). Dos capas de rescate antes de cortar: `DataIntentExtractor` (adjuntos) + `intent_rescue.py` (scoring semántico + `rapidfuzz`, normalización voseo/tildes). Se conserva `out_of_scope` para off-topic claro; ambiguo de negocio → `pedir_aclaracion_negocio`/`_sobre_archivo`. Math determinística en `shared/analytics.py`. Handlers en los 5 agentes; 4 queries nuevas en repos. Clientes (4 intents) stub → Sprint 18. `rapidfuzz` agregado a deps. |
| 18 | ✅ | **AgentChat + migración a Sonnet 4.6 + NLP rioplatense** (commit `a230009e`): nuevo `agent_chat` que absorbe la síntesis de respuesta al usuario (reemplaza `_generate_rich_response` del ChatOrchestrator; `_format_agent_result`/`_render_session_memory`/`_format_history_turn` movidos a AgentChat). Todos los agentes migrados Haiku 4.5 → `claude-sonnet-4-6` (max_tokens subidos). `INTENT_CATALOG` reestructurado `list[str]` → `dict[str, {desc, triggers}]` (60 intents, triggers rioplatenses, system prompt dinámico). `nlp_preprocessor.py`: normalización de lunfardo + spacy opcional antes del CEO. Google MCP: allowlist corregida (eliminado alias `agent_cash`). Limpieza repo: `.venv/`+`.claude-flow/` sacados del índice. 650 tests passing. |
| 19 | ✅ | **Consolidación de intents 60→28 (granularidad a entidades):** las 35 variantes analíticas (mismo agente + mismo ActionType) colapsan a 8 familias; el sub-análisis se mueve a la entidad `analysis_type`. `generar_informe`→`consultar_estado_negocio`; `ayudar_con_archivo`/`explicar_que_puedo_hacer_con_datos`→`ayuda_plataforma`. Espacio de decisión del clasificador: ~60→~25 intents sin hermanos solapados. Mecanismo: `_ANALYTIC_FAMILIES` + `_resolve_legacy_discriminator()` traduce `analysis_type`→`_intent` legacy (handlers **intactos**); `_LEGACY_INTENT_ALIASES` cubre keys en vuelo. `intent_rescue.rescue_intent()` ahora devuelve `tuple[intent, entities]`. Smoke clasificador: 20/20. 699 tests passing, cobertura 66.77%. |
| 20 | ✅ | Filtros temporales jerárquicos (ventas/gastos), horarios laborales (BusinessProfile + onboarding + settings, mig `20260616_0001`), cierre de caja con arqueo + neteo de efectivo (`cash_closes`, mig `20260617_0001`). |
| 21 | ✅ | Mapeo inteligente de columnas (`tenant_column_mappings` mig `20260620_0001`, `ColumnMapper`, `ColumnMapperPanel.tsx`) + fix rescate de adjuntos en chat (Redis `pending_file:{conversation_id}` TTL 30min). |

## Post-Sprint 21 — proyectos mergeados a main

| Proyecto | Descripción |
|----------|-------------|
| Mapeo universal de imports | `mapping_contexts[]` en `parsed_summary_json` (por hoja/grupo en todos los formatos) + tablas dinámicas desde field-definitions. |
| Transaction datetime | `transaction_date` DATE→DATETIME + `products.acquired_at` (mig `20260625_0001`). Usar `func.date()` en queries de rango. |
| Remediación de ingestión F0–F3 (PR #4) | F1: CSV sin límite `[:50]`, delimitador `;`, hojas preservadas. F2: LLM 4ª capa de mapeo + detección de tipo por contenido + traza `pipeline_events` (mig `20260701_0001`) + UI clarificación inline. F3: `expense_entries.product_id` (mig `20260705_0001`), productos `requires_completion` (mig `20260705_0002`), sync `InventoryBalance`. |
| Resumen económico F4 (PR #5) | `GET /economic-summary` + frontend `/resumen-economico`. Sin migraciones. |
| Diagnóstico MCP F5 (PR #6) | `GET /integrations/google/diagnostics` (SUPERADMIN) + runbook `docs/runbooks/google_mcp_oauth.md`. El código MCP está completo; el fallo OAuth restante es operacional (Google Cloud Console). |
| Libro Diario + pagos canónicos | Parser doble encabezado en `file_parsing.py`, `PaymentMethod.ACCOUNT`, mercadería→COGS por vertical, scripts `void_misclassified_imports.py` + `backfill_inventory_balances.py`. |
| Categorías COGS + Otros | Catálogo 13 categorías (`domain/expense_categories.py`), `expense_type` COGS/OPEX (mig `20260710_0001`), `unclassified_records` + `/others` + `/otros` (mig `20260712_0001`), importación masiva de sugeridos. |
| Reforma de Proveedores | Marca ≠ proveedor (catálogos no crean suppliers; sentinela "No identificado"), ficha fiscal (`last_name`/`cuil`/`payment_method`, mig `20260720_0003`), `inventory_movements.supplier_id` (mig `20260720_0004`) + `GET /suppliers/{id}/products`, página `/suppliers/[id]`, scripts de limpieza/backfill (categoría `MERCH_SOURCE_NO_CONTACT`). |
| Remito de proveedor | `POST /suppliers/{id}/receipts` (manual → Product+stock+COGS+envío LOGISTICS) + `POST /suppliers/{id}/receipts/extract` (`remito_extraction_service.py`: planilla determinística / foto-PDF con Claude multimodal, solo sugiere). `source_upload_id` liga archivo→gastos. Sin migraciones. |
| Reforma de Clientes | Ficha fiscal (`customer_type`/`last_name`/`doc_type`/`dni`/`cuit`/`iva_condition`/domicilio/`birthday`, mig `20260721_0001`), centinela "Local" (ventas sin cliente; `models/_sentinel.py` compartido), ruteo en `sales.py` (create/update/bulk/import → "Local"; fiado sin cliente → 400), validadores AR en `schemas/_ar_fiscal.py`. Carga por archivo: `POST /customers/extract` (`customer_extraction_service.py`, Claude multimodal, solo sugiere) + `POST /customers/import/preview`/`confirm` (`customer_import_service.py`, match DNI/CUIT, upsert idempotente). Frontend: form Persona/Empresa, página `/customers/[id]`, columna Cliente en `/sales`, `lib/fiscal.ts`. Script `backfill_local_customer.py`. |

## Cadena de migraciones (recientes)

| Migración | Qué agrega |
|-----------|-----------|
| `20260401_0003` | `business_heuristic_overrides`, `agent_conversation_context`, `heuristics_version` |
| `20260406_0001/0002` | no-op stubs (continuidad Alembic) |
| `20260421_0001/0002` | tablas de memoria (`business_memory`, `agent_memory`) |
| `20260424_0001` | `google_mcp_connections` (ORM: `GoogleMcpConnection`) |
| `20260424_0002` | `google_oauth_tokens` (solo MCP server, sin ORM en backend) |
| `20260427_0001` | `analytics_events` insert-only sin tenant_id (ORM: `AnalyticsEvent`) |
| `20260429_0001` | `tokens_input/output/total` en `decision_audit_log` + índice `(tenant_id, created_at)` |
| `20260430_0001` | índice `is_active` en `products` |
| `20260430_0002` | `password_reset_tokens` |
| `20260503_0001` | columna `provenance VARCHAR(10)` en `sales_entries` + `expense_entries` |
| `20260503_0002` | `chat_session_log` (ORM: `ChatSessionLog`) — 4ª capa de memoria |
| `20260508_0001` | custom fields: `custom_fields JSONB` en 4 tablas core + `vertical_field_definitions` + `tenant_custom_field_definitions` + `tenant_field_change_log` |
| `20260510_0001` | soft delete en `sales_entries` (`voided_at`, `void_reason`) + tablas `data_repair_runs` + `data_repair_items` |
| `20260511_0001` | soft delete en `expense_entries` + `products` (`voided_at`, `void_reason`); amplía check constraint de `data_repair_items` |
| `20260512_0001` | `low_stock_threshold_units` nullable en `products` (NULL = no configurado; 0 = umbral explícito sin alerta) |
| `20260601_0001` | `approval_group_id` + `group_execution_status` en `pending_actions` (Stage 3 multi-task) |
| `20260615_0001` | `score_growth INTEGER NULL` en `health_score_snapshots` (NULL = snapshot v1 pre-Stage-5a; NOT NULL = fórmula v2 con 5 dims) |
| `20260616_0001` | horarios laborales (`work_schedule`) en business profile |
| `20260617_0001` | `cash_closes` (ORM: `CashClose`) — cierre de caja con arqueo |
| `20260620_0001` | `tenant_column_mappings` (ORM: `TenantColumnMapping`) — aliases de mapeo de columnas por tenant |
| `20260625_0001` | `transaction_date` DATE→DATETIME en ventas/gastos + `products.acquired_at` |
| `20260701_0001` | `pipeline_events` (ORM: `PipelineEvent`) + auditoría de archivos — traza del pipeline de ingestión |
| `20260705_0001` | `product_id` en `expense_entries` (vincula gasto de mercadería al producto) |
| `20260705_0002` | `requires_completion` en `products` (productos creados incompletos desde imports) |
| `20260710_0001` | `expense_type` COGS/OPEX en `expense_entries` + catálogo canónico de categorías |
| `20260712_0001` | `unclassified_records` (ORM: `UnclassifiedRecord`) — sección "Otros" |
| `20260715_0001` | arqueo de caja completo en `cash_closes` (`opening_float_ars`, `cash_denominations`, `voucher_expenses_ars`, `result_code`) + `business_profiles.fiscal_condition` (`String(12)`, CHECK legacy) |
| `20260716_0001` | reconcilia `fiscal_condition` a 3 valores canónicos (`monotributo`/`responsable_inscripto`/`informal`): ensancha a `Text`, normaliza legacy `'registered'`→`'monotributo'`, reemplaza el CHECK. **Obligatoria** — el CHECK/`String(12)` de `20260715_0001` rompía el `PATCH /settings/fiscal-condition` |
| `20260717_0001` | `source_upload_id` en `sales_entries` + `expense_entries` (trazabilidad de import → archivo origen, base de la idempotencia/dedup de ingesta) |
| `20260717_0002` | amplía el CHECK de `data_repair_items.action` con `VOID_DUPLICATE` + `RECLASSIFY_EXPENSE` (reclasificación vertical-aware + dedup de ingesta) |
| `20260720_0001/0002` | relectura de archivos (constraints/estado de re-import) |
| `20260720_0003` | `suppliers` + `last_name`/`cuil`/`payment_method` (nullable) + índice único parcial del sentinela (`WHERE custom_fields->>'_sentinel' = 'true'`) |
| `20260720_0004` | `inventory_movements.supplier_id` (FK `suppliers` SET NULL) + índice `(tenant_id, supplier_id)` — base de `GET /suppliers/{id}/products` |
| `20260721_0001` | campos fiscales en `customers` (`customer_type`/`last_name`/`doc_type`/`dni`/`cuit`/`iva_condition`/`address`/`locality`/`province`/`postal_code`/`birthday`, nullable) + índice único parcial del centinela "Local" + índices únicos parciales anti-duplicado por `dni`/`cuit` (excluyen sentinela y soft-deleted) — Reforma de Clientes |

**Post-Sprint 8–9:** Email reemplazado SMTP→Resend HTTP API (`app/integrations/smtp.py` usa `httpx`). Railway bloquea port 587. Variables Railway: `RESEND_API_KEY=re_...` + `SMTP_FROM_EMAIL=noreply@vektor.app`. `SMTP_PASSWORD` es alias legacy.
