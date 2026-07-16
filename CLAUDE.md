# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Start here — invariantes que evitan bugs

Leé esto antes de tocar código. El detalle completo está más abajo.

1. **Chat** entra siempre por `ChatOrchestrator` → AgentCEO clasifica → sub-agente. El cliente nunca elige el agente destino.
2. **Aritmética financiera** SOLO por `DeterministicFinance`. Los LLM nunca calculan montos.
2b. **Motor estadístico** (`shared/stats_engine.py`): calcula con numpy + numpy-financial; el LLM solo narra. scipy/statsmodels diferidos. Guardas de muestra (n < mínimo → `insufficient_data`, no-invention rule).
2c. **Métricas de negocio** (ventas, márgenes, caja, fiado, stock): SOLO por **FactsService** (`app/application/services/facts_service.py` + `facts_provider.py`, pandas determinístico). Es la única fuente de verdad — dashboard, chat y health deben leer el MISMO `BusinessFact`; nadie más agrega (ni el frontend, ni SQL inline en endpoints). Estado: fase 1 lista (servicio + provider + test de reconciliación); el cableado de consumidores está en curso — mientras tanto `DeterministicFinance` sigue vigente para la aritmética puntual del chat y NO se le agrega métrica nueva a ningún otro lado.
2d. **`inventory_movements.occurred_at`** = fecha de NEGOCIO; `created_at` = fecha de carga. Toda heurística temporal sobre el ledger usa `COALESCE(occurred_at, created_at)`; para pares de movimientos importados, el timing de inserción NUNCA es evidencia suficiente de duplicado (incidente don pedro 2026-07).
3. **`tenant_id`** sale del JWT en cada query de negocio — nunca del body/path.
4. **`ActionType`** es un set cerrado (31 valores en `shared/schemas.py`); agregar/quitar obliga a tocar `RiskEngine` + tests.
5. **No-invention:** con `confidence == LOW` (`data_completeness < 50`) → empty state y pedir datos. Nunca maquillar scores con defaults (`or 70`, `or 50`); si `0` es válido, usar `if value is None`, no `or`.
6. **Auditoría + defensa:** toda decisión → `decision_audit_log` (insert-only); todo input de usuario a un LLM → `wrap_user_input()`.
7. **Mostrar plan antes de escribir código y esperar confirmación.** Responder en español (Argentina).

> Historial de sprints y cadena de migraciones: `docs/CHANGELOG-internal.md`.

---

## Proyecto

Véktor es una plataforma SaaS de salud financiera para PYMEs argentinas (kioscos, decoración hogar, limpieza). Multi-tenant, monolito modular en v1.

### Estado actual importante

- El chat productivo entra por `ChatOrchestrator`; no despacha directo desde el router al sub-agente.
- Los clientes Anthropic se construyen vía `app/integrations/anthropic_client.py`; no instanciar `anthropic.AsyncAnthropic()` directo en agentes.
- Integraciones Google son **MCP-based** (`ENABLE_GOOGLE_MCP_TOOLS` + `MCP_SERVER_URL`). `Google Login` social vía `/auth/oauth/google/*` es independiente — no confundir.
- Frontend `/apps`: conexión Google, estado de integraciones, reconnect cuando una acción devuelve `REQUIRES_RECONNECT`.
- Pipeline de archivos centralizado en `app/application/services/file_parsing.py`. Uploads de chat: parseo síncrono. Ingestión: pipeline propio con confirmación humana.
- Cadena Alembic incluye `20260401_0003_restore_chat_context_and_heuristics.py` y stubs `20260406_0001/0002_stub.py`.
- Frontend: tema dark unificado (`vektor-night`, `vektor-ink`, `vektor-surface`, `vektor-border`), tipografías `Barlow Condensed` + `Inter`.
- Landing pública (`/`): hero full-screen, social proof, highlights, y **`ScreenshotCarousel`** con 6 capturas reales en la sección "Vista previa" (el demo público autoservicio fue eliminado — ver bullet "Demo público deshabilitado").
- Dashboard dividido: `/dashboard` (health score + KPIs) y `/dashboard/analisis` (charts + insights), navegación launchpad compartida.
- Ticker económico alimentado por `GET /api/economia`. Insights de charts: `GET /insights/current` (template-based). El stub `GET /api/analisis/insight` ya no se usa.
- Benchmarks de margen desde JSON (`heuristics/verticals/loader.py → load_margin_benchmark()`). El health engine no importa los .py de vertical. Alias `kiosco` → `kiosco_almacen` normalizado en el loader.
- `HealthAlertBanner.tsx`: alertas fixed bottom-right cuando `score < 75` y `risk_code` activo (CASH_LOW, MARGIN_LOW, STOCK_CRITICAL, SUPPLIER_DEPENDENCY). Dismissable, 800ms delay.
- `SmartTable<T>` (`src/components/ui/SmartTable.tsx`): selector de columnas + exportación CSV UTF-8 BOM. Columnas con `defaultVisible` y `csvValue`.
- `GET /insights/breakdown?days=N`: gastos por categoría, top 5 proveedores, stock crítico. Alimenta dos panels en `DashboardAnalysisScreen`.
- `GET /forecast/cash`: 3 tiers según historial (Tier 1 <30d promedio 14d, Tier 2 30–90d EWMA, Tier 3 90d+ patrón semanal). Redis TTL 6h; `?refresh=true` fuerza recálculo.
- `analytics_events`: log insert-only anonimizado por vertical (sin `tenant_id`). `AnalyticsRepository.compute_margin_benchmark()` usa `percentile_cont` con ≥5 muestras/90 días para benchmarks data-driven. `GET /admin/analytics/benchmarks` (SUPERADMIN).
- `decision_audit_log`: columnas `tokens_input/output/total` (INTEGER DEFAULT 0) + campos en `decision_data`: `ceo_target_agent`, `sub_agent_name`, `token_calls`. Migración: `20260429_0001`.
- **DeterministicFinance** (`app/application/services/deterministic_finance.py`): aritmética financiera puntual del chat. LLMs nunca calculan números — siempre delegar a un servicio determinístico.
- **FactsService** (`app/application/services/facts_service.py` + `facts_provider.py`): única fuente de verdad de MÉTRICAS de negocio (`BusinessFact`: ventas período, ticket, margen bruto/neto, caja líquida, fiado, valor de stock, sobrestock). Decisiones clavadas como código: bruto≠neto, heurística sobre neto, fiado y tarjeta de crédito fuera de caja, cobros `"inflow"` fuera de ventas/dentro de caja, borde superior duro (nunca futuro), sin blend de onboarding, EMPTY→`value=None` (nunca $0 falso), confidence por cobertura de datos. Umbrales de severidad por vertical vía `thresholds_for_vertical()` (misma fuente JSON que el health engine). Compuerta: `app/tests/application/test_facts_reconciliation.py` valida contra las decisiones, no contra números viejos. Métrica nueva ⇒ acá + caso en la reconciliación, nunca en endpoints/frontend/agentes.
- **ValidationGate** (`app/application/services/validation_gate.py`): rechaza `confidence=LOW` antes de que el agente procese datos. Sprint 11.
- **Provenance:** columna `provenance VARCHAR(10)` en `sales_entries`/`expense_entries` (migración `20260503_0001`). Demo data marcada como `"demo"`; real data como `"manual"` o `"import"`.
- **ChatMemoryService** (`app/application/services/chat_memory_service.py`): 4ª capa de memoria — log durable de eventos por sesión (`chat_session_log`). Entry types: `DATA_LOADED`, `DATA_REJECTED`, `FILE_UPLOADED`, `INTENT_DETECTED`, `QUERY_ANSWERED`. Sprint 11.
- **Custom Fields** (Sprint 12): columna `custom_fields JSONB` en `sales_entries`, `expense_entries`, `products`, `business_profiles`. Definiciones base en `vertical_field_definitions`; overrides por tenant en `tenant_custom_field_definitions`; undo log en `tenant_field_change_log`. API: `/fields`. Gestión en frontend: `FieldDefinitionsPanel.tsx` + `SchemaERDView.tsx` en `/settings`.
- **Agent Automation Rules** (`app/application/services/automation_service.py`): reglas de consentimiento explícito para auto-ejecutar acciones recurrentes. Feature flag: `ENABLE_AGENT_AUTOMATIONS`. API: `/automations`. Sprint 11.
- **Soft delete auditado** (Sprint 13): columnas `voided_at` + `void_reason VARCHAR(30)` en `sales_entries` y `expense_entries`. `void_reason` acepta: `REPAIR_MISCLASSIFIED_IMPORT`, `USER_REQUEST`, `DATA_QUALITY`. Migraciones: `20260510_0001` + `20260511_0001`.
- **DataRepairService** (`app/application/services/data_repair_service.py`): detecta `SaleEntry` creadas desde CSVs de productos mal clasificados, anula las ventas (soft delete) y reconstruye los `Product`. Flujo: `dry_run=True` → preview → `apply`. Trazabilidad en `data_repair_runs` + `data_repair_items`. API admin: `POST /admin/repairs/misclassified-product-imports/dry-run` + `.../apply` (SUPERADMIN).
- **Product lookup en AgentCash** (Sprint 13): `_lookup_product_price()` hace fuzzy match contra catálogo del tenant. Si el producto es ambiguo, el agente pregunta al usuario antes de registrar. `product_id` se inyecta en la entidad y el PATCH de ventas lo acepta y recalcula el monto.
- **Estados canónicos de producto** (Sprint 14): `ProductResponse.stock_status` computed field — `"in_stock" | "low_stock" | "out_of_stock"`. Frontend usa estos valores en badges ("En stock" / "Pocas unidades" / "Sin stock") y filtros URL (aliases `ok/low/out` siguen funcionando). Chip "En camino" es stub en dashboard (count=0, sin lógica hasta `purchase_orders`).
- **UPDATE_PRODUCT vía chat** (Sprint 14): `ActionType.UPDATE_PRODUCT` (MEDIUM). AgentCEO intent `update_product` → AgentStock `PRODUCT_UPDATE` handler. Extrae campos a modificar; distingue `sku` (identificador) de `new_sku` (campo a actualizar). `PendingActionService` ejecuta via `ProductRepository`; recalcula score solo si cambian `sale_price_ars/unit_cost_ars/stock_units`. `ChatOrchestrator` eliminó la restricción "no puede modificar datos vía chat"; incluye regla FUENTE DE VERDAD: Google Sheets solo para import/export, nunca para modificar.
- **Política null/NaN en imports** (Sprint 14): helpers en `file_parsing.py` — `normalize_numeric()`, `normalize_categorical()`, `impute_column()` (quantity→0, numeric→mediana, categorical→None), `compute_column_null_stats()`, `flag_columns_at_risk()` (umbral 35%). `_parse_spreadsheet` calcula `columns_at_risk` y lo incluye en `parsed_summary_json`. Validators `amount_no_nan` en `CreateSaleRequest` y `CreateExpenseRequest`.
- **Ingesta interactiva para columnas riesgosas** (Sprint 14): `GET /ingestion/files/{id}/preview` expone `columns_at_risk[]`. `POST /files/{id}/drop-columns` elimina columnas de todos los buckets del summary (ventas/gastos/productos/stock_detectado). `POST /files/{id}/cancel` marca el archivo como `NEEDS_COMPLETION`. Frontend `FileListSection` muestra panel por cada columna >35% nulos con botones "Eliminar columna" y "Cancelar y completar datos".
- **Horarios laborales + cierre de caja** (Sprint 20): `work_schedule_service.py` (BusinessProfile + onboarding + settings, migración `20260616_0001`) y `cash_close_service.py` con arqueo y neteo de efectivo (`cash_closes`, migración `20260617_0001`). Router `/cash-closes`: `GET /preview`, `POST` (crear cierre), `GET` (listar). Filtros temporales jerárquicos en ventas/gastos.
- **Mapeo inteligente de columnas** (Sprint 21): `column_mapping_service.py` (`ColumnMapper`) con aliases por tenant en `tenant_column_mappings` (migración `20260620_0001`). Frontend: `ColumnMapperPanel.tsx`. Rescate de adjuntos en chat: Redis `pending_file:{conversation_id}` TTL 30min.
- **Mapping contexts universales:** `parsed_summary_json` incluye `mapping_contexts[]` (mapeo por hoja/grupo en todos los formatos — spreadsheet, texto, imagen) + tablas dinámicas desde field-definitions.
- **`transaction_date` es DATETIME** (migración `20260625_0001`, también `products.acquired_at`): soporta intradía. En queries de rango/agregación por día usar `func.date()`.
- **Remediación de ingestión (FASES 0–3, PR #4):** FASE 1 fixes — CSV sin límite de filas (eliminado `[:50]`), detección de delimitador `;`, hojas no clasificables preservadas. FASE 2 — `llm_column_mapper.py` (4ª capa de mapeo LLM), `llm_file_type_detector.py` (detección de tipo por contenido), traza en `pipeline_events` (`pipeline_event_service.py`, migración `20260701_0001`), UI de clarificación inline. FASE 3 — `expense_entries.product_id` (mig `20260705_0001`), productos `requires_completion` (mig `20260705_0002`), sync `InventoryBalance` (`inventory_balances`).
- **Resumen económico analítico** (FASE 4): `GET /economic-summary` (`economic_summary_service.py`) + frontend `/resumen-economico`. Stock valuado = `stock_units × unit_cost_ars`; `missing_cost` solo con stock>0; doble disclaimer (NO es informe contable). Sin migraciones.
- **Parser Libro Diario** (`file_parsing.py`): `detect_libro_diario_header()` + `parse_libro_diario()` — doble encabezado Dinero/Mercadería × Ingreso/Egreso. Las hojas derivadas del Libro Diario no se importan además (duplicarían ventas). `PaymentMethod.ACCOUNT` + normalización de pagos canónicos en ventas importadas.
- **Categorías de gasto canónicas + COGS/OPEX:** catálogo de 13 categorías en `app/domain/expense_categories.py`; `infer_expense_type()` discrimina `COGS` (product_id vinculado o categoría `INVENTORY`) vs `OPEX` (migración `20260710_0001`). Gastos de mercadería del vertical → INVENTORY/COGS. **También en gastos manuales** (PR #14, `43893ec0`): `POST /expenses` y la clasificación en `others.py` pasan por `infer_expense_type()` (un gasto manual categoría `INVENTORY` → COGS), no solo los imports.
- **Sección "Otros"** (`unclassified_records`, migración `20260712_0001`): filas ambiguas de imports ya NO caen a ventas por default — van a `unclassified_records` con `suggested_entity`. Router `/others`: listar, `GET /count`, clasificar, importación masiva de sugeridos. Frontend `/otros` con badge de sugerencia.
- **Diagnóstico MCP Google** (FASE 5): `GET /integrations/google/diagnostics` (SUPERADMIN) vía `mcp_diagnostics_service.py` — chequea flag/URL/secret/conectividad/auth. Runbook: `docs/runbooks/google_mcp_oauth.md`.
- **Régimen fiscal** (`business_profiles.fiscal_condition`): 3 valores canónicos `monotributo | responsable_inscripto | informal` (+ NULL = no configurado). **Solo informativo** (mejora heurísticas + guía del arqueo), nunca bloquea. Normalizador tolerante a legacy en `app/domain/fiscal_condition.py` (`'registered'`→`'monotributo'`); `GET/PATCH /settings/fiscal-condition` y `cash_close_service` normalizan al leer. Opcional en onboarding. Frontend: `lib/fiscalCondition.ts` (+ `FISCAL_PRIVACY_NOTE`, copy Ley 25.326/AAIP), `FiscalConditionPanel`. Migración `20260716_0001` (obligatoria; ver tabla).
- **Compra de mercadería = COGS + producto + stock** (`ingestion_import_service.py`): un libro de compras ruteado a gastos crea el `Product` además del gasto COGS+caja, vía el helper compartido `build_incomplete_product()` (`requires_completion=True`, `sale_price_ars=0`). Gate estricto (`expense_type=="COGS"` + nombre + cantidad>0) para NO crear productos basura desde OPEX. `_ensure_product_for_purchase()` delega en el helper. El clasificador (`file_parsing.infer_spreadsheet_type`) ya distingue libro de compras (total+forma_pago+proveedor+fecha) de catálogo de productos.
- **Reclasificar gasto vía chat** (`ActionType.RECLASSIFY_EXPENSE`, MEDIUM): intent `reclasificar_gasto` → `agent_expense`. El handler asesora **reventa** (COGS/INVENTORY → producto vendible) vs **insumo** (OPEX/SUPPLIES) vs otra categoría según el vertical, y al confirmar reclasifica `category`+`expense_type` (y crea producto vendible si reventa) vía `pending_action_service._execute_reclassify_expense` + `ExpenseRepository.reclassify`. `intent_rescue.OBJECTS_CLASIFICACION` evita que una consulta de categorización caiga en `out_of_scope`.
- **Reforma de Proveedores (marca ≠ proveedor):** los catálogos de productos ya NO crean proveedores — la marca queda en `Product.custom_fields["marca"]`. Una compra sin proveedor informado se agrupa en un único **sentinela "No identificado"** por tenant (flag `custom_fields["_sentinel"]`, índice único parcial, helper centralizado `is_sentinel_value`/`Supplier.is_sentinel` en `models/supplier.py`). Campos de ficha: `last_name`/`cuil`/`payment_method` (persona o razón social). `inventory_movements.supplier_id` alimenta `GET /suppliers/{id}/products` (`InventoryRepository.products_purchased_from_supplier`: última compra/cantidad/precio unit). Frontend: detalle a **página `/suppliers/[id]`** (no modal), nombre navegable, mailto/WhatsApp, helpers en `frontend/src/lib/suppliers.ts`.
- **Remito de proveedor** (`POST /suppliers/{id}/receipts`): alta manual de líneas → crea Product+stock+`ExpenseEntry` COGS por línea + envío→OPEX `LOGISTICS`, idempotente, validaciones 422 (qty entero, unit_price≥0). **Lectura por archivo** (`POST /suppliers/{id}/receipts/extract`, `remito_extraction_service.py`): planillas XLSX/CSV se parsean determinísticamente (reusa `normalize_numeric`/`ColumnMapper`); foto/PDF con **Claude `sonnet-4-6` multimodal + structured output** (transcribe, NO calcula; fail-soft). Solo SUGIERE (human-in-the-loop): prellena el `ReceiptModal`, el usuario confirma. Guard: no se puede cargar/leer remito contra el sentinela. `source_upload_id` liga el archivo a los `ExpenseEntry` creados.
- **Reforma de Clientes (ficha fiscal + cliente "Local"):** espeja la Reforma de Proveedores. Columnas fiscales nullable en `customers` (`customer_type` person/company, `last_name`, `doc_type`, `dni`, `cuit` —antes `cuil`—, `iva_condition`, `address`/`locality`/`province`/`postal_code`, `birthday`; mig `20260721_0001`). Centinela **"Local"** por tenant (`custom_fields["_sentinel"]="true"`, índice único parcial) para ventas sin cliente identificado; el helper `is_sentinel_value`/`SENTINEL_FLAG_KEY` se extrajo a `models/_sentinel.py` (compartido por `Customer` y `Supplier`). `customer_sentinel.py`: `resolve_or_create_local_sentinel` (get-or-create, `IntegrityError`→re-query) + `assign_orphan_sales_to_local`. `CustomerRepository.list_by_tenant`/`count_active`/`get_inactive_customers` **excluyen** el centinela. Ruteo en `sales.py`: las 3 rutas de venta (create/update/bulk) + import sin `customer_id` → "Local"; **fiado** (`payment_method="account"`) sin cliente real → **400**. Centinela protegido (no editable/borrable). Validadores fiscales AR extraídos a `schemas/_ar_fiscal.py` (`validate_cuit`/`cuit_check_digit_ok` módulo 11, `validate_dni` `^\d{7,8}$`), reusados por customer+supplier. Obligatoriedad backend (`missing_required_fields()` → 422 `CUSTOMER_INCOMPLETE`) con excepción `allow_incomplete` para centinela/import. **Carga por archivo (Fase B):** `POST /customers/extract` (`customer_extraction_service.py`: foto/PDF → Claude `sonnet-4-6` multimodal, planilla de 1 cliente determinística; solo SUGIERE, prellena el form, NO persiste; guard 413) + `POST /customers/import/preview`/`POST /customers/import/confirm` (`customer_import_service.py`: matchea por DNI/CUIT → to_create/to_update/invalid/duplicates; confirm upsert idempotente; el centinela nunca se crea por import). Frontend: form Persona/Empresa, página de detalle `/customers/[id]` (resumen comercial + historial), columna **Cliente** en `/sales`, `CustomerFileModal` (extracción individual + import masivo), helpers en `lib/fiscal.ts`. Script `backfill_local_customer.py` (reasigna ventas históricas sin cliente → "Local").
- **PIN de seguridad (step-up auth):** segundo factor de 4 dígitos **por usuario** para acciones sensibles. Columnas en `users`: `pin_hash`/`pin_set_at`/`can_modify_sensitive` (mig `20260723_0001`, additive). `PinService` (`pin_service.py`) reusa `hash_password`/`verify_password`; ventana de verificación en Redis (`redis_client.get_redis`, key `pin:verified:{tenant_id}:{user_id}` TTL 10 min) **fail-closed** (error de Redis → no verificado); lockout por usuario tras 5 fallos (15 min); mensajes genéricos; el PIN nunca se audita. Endpoints `POST/GET /auth/pin/{status,setup,verify,change,reset}`; logout/change-password invalidan la ventana. **Dos dependencies separadas** en `deps.py`: `require_modify_access` (OWNER **o** `can_modify_sensitive` + ventana → editar/borrar datos + config) y `require_owner_stepup` (OWNER estricto + ventana → forzar baja con historial, reactivar, permisos de equipo, `DELETE` de usuarios). Sin ventana → **428 `PIN_REQUIRED`** (el interceptor frontend abre el modal y reintenta 1 vez, single-flight, no intercepta `/auth/pin/*`). **Gated** (`require_modify_access` salvo nota): PATCH/DELETE de customers/suppliers/sales/expenses/products; settings health-config/work-schedule/fiscal-condition; tenants PATCH; integrations connect/disconnect; ingestion `reread/apply`; `POST /expenses/profit-withdrawal`; fields PATCH/toggle/undo; automations PATCH/DELETE; marketing metrics PATCH/DELETE. **`require_owner_stepup`** (OWNER estricto, NO alcanza `can_modify_sensitive`): `GET/PATCH /settings/team`, **users POST/PATCH/DELETE** (gestión de roles → evita auto-escalada a OWNER; excepción: `PATCH /users/me` = perfil propio, solo `full_name`/`phone`, sin PIN), force-delete con historial, reactivar. **NO gated:** altas/data-entry (POST de venta/gasto/compra/cierre/remito/métrica), business_profiles PATCH (onboarding). `/auth/pin/{verify,change,reset}` tienen `@limiter` + el lockout de `PinService` (5 fallos/15min) aplica a verify **y** change/reset. **ADMIN existente:** la migración backfillea `can_modify_sensitive=true` para `role_code='ADMIN'` (continuidad); los ADMIN nuevos arrancan en false y el OWNER decide. El OWNER otorga permiso a sub-cuentas en `/settings` → **Seguridad** (`SecurityPanel`); cada sub-cuenta tiene su propio PIN. Frontend: `PinGateModal` + `pinGateStore` (montado en el layout protegido, auto-setup si `must_set`).
- **Borrado protegido + inactivos visibles + reactivar:** clientes/proveedores con historial NO se borran salvo `force=true` (solo OWNER + PIN) → soft-delete; sin force → **409 `HAS_HISTORY`**. `CustomerRepository.count_sales` / `SupplierRepository.count_history` detectan historial. `list_by_tenant(include_inactive)` y `get_by_id(include_deactivated)` para mostrar/abrir inactivos (en `GET /customers|suppliers?include_inactive=true`; detalle abrible, NO 404). `POST /{id}/reactivate` (OWNER+PIN) limpia `deactivated_at`. Guard del sentinela en delete (Local / No identificado). Frontend: fila inactiva en rojo + badge `danger`, botón Reactivar.
- **Marcas colapsadas ocultas (`_brand_collapsed`):** las marcas confundidas con proveedores que colapsó `deactivate_brand_suppliers.py` llevan `custom_fields["_brand_collapsed"]="true"` (constante `BRAND_COLLAPSED_FLAG_KEY` + `Supplier.is_brand_collapsed` + computed en `SupplierResponse`). NO son bajas de negocio: `SupplierRepository.list_by_tenant` las excluye **incluso con `include_inactive=true`** (helper `_not_brand_collapsed_or_active()`, cast cross-dialect PG/SQLite) y `POST /suppliers/{id}/reactivate` las rechaza con **409 `BRAND_COLLAPSED`** (la vía de restauración es el script de revert, que limpia el flag). El detalle por id sigue abrible (debug). Backfill de las colapsadas históricas: `scripts/backfill_brand_collapsed_flag.py` (lee la traza de `decision_audit_log`).
- **Teléfono/WhatsApp del usuario** (`users.phone`, mig `20260730_0001`): opcional en el registro (`RegisterRequest.phone`) y editable en `/settings` → Cuenta vía **`PATCH /users/me`** (self-service, solo `full_name`/`phone`, sin PIN; `role_code`/`email` quedan fuera del schema). Expuesto en `UserResponse`/`UserInAuthResponse`/`MeResponse` y en el `authStore` del frontend (`updateUser`, sesiones viejas hidratan desde `/auth/me`). Informativo — los botones de contacto (`wa.me`/Gmail compose en `ContactCommunication`) son redireccionadores y no lo necesitan.
- **Retiro de ganancias anticipadas:** `POST /expenses/profit-withdrawal` (`require_modify_access`) registra el sueldo/retiro del dueño como gasto `PAYROLL`/`OPEX` (`custom_fields.profit_withdrawal=true`); el monto lo fija el usuario (no se calcula). El `POST /expenses` común **no** pide PIN. Frontend: botón en el Balance (`EconomicSummaryScreen`) → `ProfitWithdrawalModal` (monto + fecha + método + nota).
- **Chequeo de integridad de stock** (`inventory_integrity_service.py`): reconstruye el stock esperado (ancla `catalog_initial_stock` + compras − ventas vivas), extendido a sumar `adjustment` taggeados (`source_type in (reconciliation, manual_adjustment)`, blindados por el CHECK `20260728_0001`) y `loss` (merma, ya negativa en el ledger); saltea productos con `sale`/`return` en el ledger o `adjustment` sin `source_type` (legacy no auditable). Nunca auto-corrige (no-invention). `GET /admin/inventory-integrity/{tenant_id}` (SUPERADMIN) + job Celery semanal `inventory_integrity_check` (todos los tenants, notifica OWNER + audita).
- **Descuento de stock en ventas en vivo + stock negativo prohibido** (PR #12, `1647fd35`, mig `20260729_0001_live_sale_stock_idempotency`): una venta con `product_id` descuenta stock al confirmar, vía helper idempotente `stock_service.decrement_for_sale` (+ reversa), disparado por evento `after_commit`. **STOCK NEGATIVO PROHIBIDO:** se valida ANTES de persistir/descontar (`check_stock_available` / `validate_sale_update_stock`) y se rechaza con `InsufficientStockError` → handler 400 (en chat via `user_message`); el `clamp` de `decrement_stock` queda como red inerte para ventas ya validadas. Bulk suma por producto; PATCH aplica efecto neto sin re-mutar; ventas importadas con `source_upload_id` NO auto-descuentan (ya vienen con su ledger). `product_id` inexistente → 400, no 500. El integrity check ignora los `sale` del ledger para no doble-contar.
- **Warnings human-in-the-loop al confirmar import** (PR #13, `1e2bb509`): `ingestion.py` arma `ConfirmIngestionResponse.warnings: list[str]` desde `counts` (`sin_proveedor` → compras colapsadas al sentinela "No identificado"; `sin_producto` → creó producto incompleto; otros). Frontend muestra banner/toasts al confirmar. No bloquea — informa qué se resolvió automáticamente.
- **Reconciliación temporal de stock** (PR #15 `af172603` + PR #16 `3f0d5698`): `inventory_temporal_service.py` detecta ventas importadas que exceden el stock reconstruible **por fechas** — `replay_timeline()` (pura: ancla de apertura por valor ignorando fecha, tie-break crédito-antes-que-débito) + `check_products_temporal_divergence()`. Clasificación de movimientos centralizada en `inventory_movement_origin.classify_stock_movement(movement_type, source_type)` (dedup del code-review). Script `reconcile_historical_sales_vs_stock.py` (dry-run `--all-active --out r.csv` → revisar → `--audit`), que reporta cobertura (checked/skipped, no solo divergencias). **Preventivo/forward-looking:** depende del ancla `catalog_initial_stock`, que no existe en datos históricos → cobertura 0 sobre lo viejo (esperado). Ver invariante 2d.
- **Demo público deshabilitado** (PR #17 `c6ffc300` frontend + PR #18 `409ce492` backend): se eliminó la página `/(public)/demo/page.tsx` (ahora 404) y el botón "Ver demo" del hero (secundario → "Ya tengo cuenta"/`/login`); la sección "Vista previa" usa `ScreenshotCarousel` con 6 capturas reales en `frontend/public/screenshots/` (scroll-snap sin deps nuevas, flechas/dots, respeta `prefers-reduced-motion`). Backend: helper `_is_demo_auth_blocked()` en `auth` — tenants `is_demo` solo autentican con `DEMO_MODE`/`DEBUG`; en prod `login`/`refresh`/`verify-email` contra tenant demo se rechazan con errores genéricos (401/401/400). Sin migración; el demo local sigue funcionando.

---

## Quick start (fresh clone)

```bash
# Backend
cd backend && cp .env.example .env && make dev && make seed-demo

# Frontend (otra terminal)
cd frontend && cp .env.local.example .env.local && npm install && npm run dev
```

Abrir `http://localhost:3000/demo` para entrar con un tenant demo.

---

## Comandos de desarrollo

### Backend (correr desde `backend/`)

```bash
make dev                    # Docker Compose con hot reload
make dev-bg / make stop / make logs / make shell

# Tests
make test                   # pytest con cobertura (mínimo 50%)
make test-fast / make test-watch
make test-file FILE=app/tests/api/v1/test_auth.py
pytest app/tests/api/v1/test_auth.py::test_login -v

# Calidad
make lint / make format / make typecheck / make check

# Migraciones (dir: backend/app/persistence/migrations/versions/, NO backend/alembic/versions/)
make migrate / make migrate-down   # contra Docker Postgres local
make migrate-neon                  # contra Neon (producción) — pide DATABASE_URL del shell
make migrate-create MSG="descripcion"
make migrate-history

# Demo
make seed-demo / make reset-demo
make db-reset               # ⚠️ PELIGROSO
make seed-vertical-fields   # Upsert definiciones de campos por vertical desde JSON (idempotente)
```

### Scripts operativos (`backend/scripts/`)

- `diag_account.py` / `diag_expenses.py` — diagnóstico de cuentas reales: **read-only, solo SELECT, nunca imprimen la connection URL**. La `DATABASE_URL` la provee el usuario desde su shell (el `.env` está bloqueado para lectura).
- `void_misclassified_imports.py` — anula (soft delete auditado) registros importados mal clasificados.
- `backfill_inventory_balances.py` / `backfill_expense_categories.py` — backfills vertical-aware.
- `reanalyze_uploaded_files.py` — re-corre el pipeline de parsing sobre archivos ya subidos (`--include-imported` disponible).
- `deactivate_brand_suppliers.py` — limpieza reversible de marcas-proveedor (dry-run/`--apply`, `--tenant`/`--all-active`). Categorías: `BRAND_FROM_CATALOG` (matchea `custom_fields["marca"]` en ≥2 productos) y `MERCH_SOURCE_NO_CONTACT` (sin contacto + solo COGS + no creado en app → colapsa al sentinela "No identificado", mueve la marca al producto, re-apunta gastos/movimientos; reversa en `_supplier_prev`). Protege proveedores reales (OPEX o creados en app vía audit `DATA_RECORD_*`). Auditado.
- `backfill_inventory_movement_supplier.py` — puebla `inventory_movements.supplier_id` desde los gastos (solo match exacto y único; dry-run/`--apply`).
- `backfill_brand_collapsed_flag.py` — taggea con `_brand_collapsed` las marcas ya colapsadas por el cleanup histórico, leyendo `decision_audit_log` (`SUPPLIER_BRAND_CLEANUP`/`SUPPLIER_MERCH_SOURCE_COLLAPSE`); dry-run/`--apply`, idempotente, no toca suppliers reactivados a propósito. Correr contra Neon post-deploy.
- `reconcile_untagged_adjustments.py` — clasifica adjustments vivos sin `source_type` contra los documentos cargados (dry-run/`--apply`, `--tenant`/`--all-active`); backfillea procedencia o los anula con ajuste incremental de stock. `--void-keep-stock`: void ledger-only SIN mutar stock, para poblaciones donde el stock actual ya fue verificado correcto sin esos adjustments (caso don pedro: deshacerlos re-inflaría +29k unidades); la auditoría graba `stock_changes: []` para que la reversa tampoco toque stock. Audita `UNTAGGED_ADJUSTMENT_RECONCILIATION`. Desbloquea la migración CHECK `20260728_0001`. Reversa: `revert_untagged_adjustment_reconciliation.py` (aborta si la CHECK ya está aplicada).
- `detect_misvoided_purchases.py` — detector read-only de compras posiblemente mal-voideadas por el dedup (señales `distinct_uploads`/`distinct_hashes`/divergencia de integridad); `--audit` registra `INVENTORY_MISVOIDED_PURCHASE_FINDING`. NO repara (human-in-the-loop).
- `diag_missing_purchases_scope.py` / `fix_adjustment_sign.py` / `fix_reconciled_stock.py` — diagnóstico y fixes puntuales del incidente de inventario de don pedro (2026-07).
- `_db.py` — helper compartido de conexión para los scripts.

### Frontend (correr desde `frontend/`)

```bash
npm run dev / npm run build / npm run lint / npm run type-check / npm run test
```

### Variables de entorno

- Backend: `backend/.env.example` → `backend/.env`
- Frontend: `frontend/.env.local.example` → `frontend/.env.local` (`NEXT_PUBLIC_API_URL`)
- En producción: Railway/Vercel, nunca commitear.

---

## Arquitectura del backend

### Flujo de datos principal

```
HTTP Request
  → deps.py (JWT decode + tenant_id injection)
  → Router (api/v1/)
  → Application Service
  → Business State Layer (BSL)  ← agrega 30 días de transacciones
  → Health Engine (domain/health_score.py)  ← calcula score compuesto
  → Persistence (repository)
  → decision_audit_log (insert-only, siempre)
  → Celery task (score recalculation async, post-write)
```

**Regla crítica:** Datos crudos NUNCA llegan al Health Engine directamente. Todo pasa por `BusinessStateLayer.compute()` → `BusinessState` con 5 scores (0–100): `liquidity`, `profitability`, `cost_control`, `sales_momentum`, `debt_coverage`.

### Capas y responsabilidades

| Capa | Path | Responsabilidad |
|------|------|-----------------|
| API | `app/api/v1/` | Routing, validación Pydantic, auth deps |
| Deps | `app/api/v1/deps.py` | JWT decode, `get_current_user`, `get_current_tenant`, `require_role()` |
| Application | `app/application/services/` | Orquestación: auth, cash, cash_close, conversation, google_oauth, health_score, health_config, onboarding, pending_action, score_trigger, stock, supplier, business_memory, agent_memory, forecast, analytics, deterministic_finance, validation_gate, chat_memory, field_definition, automation, data_intent_extractor, ingestion_import, data_repair, report_export, team_plan_executor, help_documentation, column_mapping, llm_column_mapper, llm_file_type_detector, pipeline_event, economic_summary, work_schedule, mcp_diagnostics |
| Commands/Queries | `app/application/commands/` `app/application/queries/` | CQRS writes/reads |
| DTOs | `app/application/dto/` | Objetos de transferencia entre capas |
| DB middleware | `app/application/db/tenant_context.py` | Inyecta tenant_id en SQLAlchemy |
| **Agents** | `app/application/agents/` | Capa multiagente LLM |
| Domain | `app/domain/` | Entidades puras Python |
| BSL | `app/state/business_state_layer.py` | Agrega revenue/expenses 30 días → 5 scores |
| Heuristics | `app/heuristics/` | Reglas por vertical |
| Persistence | `app/persistence/` | SQLAlchemy async, repositories, Alembic |
| Jobs | `app/jobs/` | Celery: scores, notifications, reports, ingestion |
| Security | `app/application/security/` | `prompt_defense.py` (`wrap_user_input()`) |

### API Routers (`app/api/v1/`)

Registrados en `router.py`. Dominios: `auth`, `oauth`, `tenants`, `users`, `business_profiles`, `sales`, `expenses`, `products`, `health_scores`, `insights`, `momentum`, `notifications`, `files`, `ingestion`, `onboarding`, `agent`, `integrations`, `forecast`, `admin`, `fields`, `automations`, `settings`, `cash_closes` (`/cash-closes`), `others` (`/others`), `economic_summary` (`/economic-summary`), `suppliers` (`/suppliers`: CRUD + `GET /{id}/products` + `POST /{id}/receipts` + `POST /{id}/receipts/extract`), `customers` (`/customers`: CRUD + `POST /extract` + `POST /import/preview` + `POST /import/confirm`; centinela "Local" protegido).

**Router `settings`** (`app/api/v1/settings.py`):
- `GET /settings/health-config` — configuración de margen actual del tenant
- `PATCH /settings/health-config` — actualizar objetivos (OWNER/ADMIN); valida `target_margin_pct >= warning_margin_pct`, rango [0.0, 80.0]
- `DELETE /settings/health-config` — resetear a valores del vertical

**`POST /health-scores/{snapshot_id}/export`** — genera informe PDF o DOCX vía `report_export_service.py`. Body: `{format: "pdf"|"docx", narrative: str}`.

### Autenticación y multi-tenancy

- JWT (HS256, python-jose). Payload: `sub` (user_id), `tenant_id`, `role_code`.
- `get_current_tenant_id` se inyecta en TODOS los endpoints de negocio. El `tenant_id` viene del JWT — nunca del body/path.
- Roles: `OWNER`, `ADMIN`, `VIEWER`. Se aplica con `require_role("OWNER", "ADMIN")`.
- En producción: `/docs`, `/redoc` y `/openapi.json` están deshabilitados.

### Celery

Queues: `default`, `scores`, `notifications`, `reports`, `ingestion`.
Post-write: `trigger_score_recalculation.delay(str(tenant_id), triggered_by="...")`.
Beat schedule: momentum update + weekly email (lunes 08:00 ART).

### Scores: dos sistemas distintos

**`ScoreLevel`** (`app/domain/health_score.py`) — clasifica `total_score`:

| Rango | ScoreLevel | | Rango | Severidad (notif) |
|-------|-----------|--|-------|-----------|
| 90–100 | `EXCELLENT` | | ≥80 | `LOW` |
| 75–89 | `GOOD` | | ≥60 | `MEDIUM` |
| 60–74 | `FAIR` | | ≥30 | `HIGH` |
| 40–59 | `WARNING` | | <30 | `CRITICAL` |
| 0–39 | `CRITICAL` | | | |

`HealthScore.needs_attention` → `True` si `level in (CRITICAL, WARNING)`.
**`severity_from_score()`** en `app/heuristics/insight_templates.py` — severidad de notificación.
> Los dos sistemas son **intencionalmente distintos** y no deben unificarse.

### Heuristics e Insights

- Insights **template-based**, no LLM. Templates en `app/heuristics/insight_templates.py`.
- Risk codes: `CASH_LOW`, `MARGIN_LOW`, `STOCK_CRITICAL`, `SUPPLIER_DEPENDENCY`.
- Benchmarks canónicos: `app/application/data/heuristics/{kiosco_almacen,limpieza,decoracion_hogar}.json` (campos: `critical_below`, `warning_below`, `healthy_min`, `healthy_max`). Con ≥5 muestras/90 días, `HealthScoreService` usa el benchmark data-driven.

### Capa de Agentes LLM (`app/application/agents/`)

10 sub-agentes coordinados por AgentCEO + AgentChat (capa de respuesta). El cliente NUNCA elige el agente destino. En el registry hay 11 entradas (`agent_cash` es alias de `agent_income`).

| Agente | Context Budget | Responsabilidad |
|--------|---------------|-----------------|
| AgentCEO | 2.000 tokens | Router/coordinador — nunca accede a datos de negocio |
| AgentIncome | 3.000 tokens | Ventas, cobros, ingresos, import archivos de ventas |
| AgentExpense | 3.000 tokens | Gastos, pagos, salidas de caja, import archivos de gastos |
| AgentStock | 3.000 tokens | Inventario, quiebres, rotación, merma, actualizar productos |
| AgentSupplier | 3.500 tokens | Proveedores, Gmail vía MCP |
| AgentHealth | 4.000 tokens | Score de salud, narrativa ejecutiva |
| AgentHelper | 2.500 tokens | FAQ, manual, guía funcional |
| AgentGoogle | 4.000 tokens | Google Calendar + Sheets + Docs vía MCP |
| **AgentClient** (v4 F2/F6a) | — | Análisis de clientes (facturación, inactivos, cuentas por cobrar, priorizar cobranza) + cobranza WhatsApp (`PREPARE_WHATSAPP_MESSAGE`, LOCAL/MEDIUM, `requires_approval`; el link `wa.me` lo ejecuta `PendingActionService`). `agent_client` en el registry. |
| **AgentMarketing** (v4 F4) | — | Marketing read-only y determinístico (`ANALYZE_MARKETING_DATA`): dashboard ads/ventas, ROI de ads, sugerir campaña. El LLM nunca calcula — cifras desde `MarketingService` + `shared/analytics`. `agent_marketing` en el registry. |
| **AgentChat** | — | **Capa de respuesta (Sprint 18):** sintetiza el `AgentResponse` del sub-agente + contextos en lenguaje natural rioplatense. NUNCA accede a la DB. Reemplazó a `_generate_rich_response` del ChatOrchestrator. `agent_chat` en el registry. |

> Verificar `backend/app/application/agents/<agent>/agent.py` antes de asumir estado del agente.
> Stage 5d completado: `agent_calendar` y `agent_sync` eliminados del registry. Solo queda el alias `agent_cash` → `agent_income` (backward-compat para `PendingActions` en vuelo con target viejo).

**Modelos LLM (Sprint 18 — migración Haiku → Sonnet):** todos los agentes usan `claude-sonnet-4-6` (CEO, Income, Expense, Stock, Supplier, Helper, Google, AgentChat, AgentHealth sub_narrator y `ceo/synthesis.py`). max_tokens subidos en la migración (CEO 300→800, Income 300→800, Stock hasta 800, Supplier 600→1200, Helper 400→800, synthesis 600→1200, AgentChat 1200). La única mención de `claude-haiku-4-5-20251001` que queda es un comentario de ejemplo en `shared/schemas.py`. Verificar cada agente antes de asumir el modelo.

**Cliente Anthropic:** todos los agentes via `get_anthropic_async_client()`. No instanciar `anthropic.AsyncAnthropic` directo.

**Contratos fijos** (`app/application/agents/shared/schemas.py`):
- `AgentRequest`: `{ request_id, user_id, business_id, message, attachments, conversation_id }` — sin `agent_target`
- `AgentResponse`: `{ request_id, agent_name, status, risk_level, requires_approval, confidence, result, pending_action_id?, pending_action_ids?, approval_group_id?, question?, message?, usage? }` — `pending_action_ids`/`approval_group_id` implementados en Stage 3 (multi-task)
- `AgentRequest.context`: dict reservado para outputs upstream del DAG (`{"upstream_outputs": {task_id: result_dict}}`). Vacío en single-task y en el primer nivel.
- `status`: `"success" | "requires_approval" | "requires_clarification" | "requires_google_auth" | "error"`
- `confidence`: `"HIGH" | "MEDIUM" | "LOW"` — nunca float
- `LLMCall`: `{ source, model, input_tokens, output_tokens }`. `UsageSummary`: `{ calls: list[LLMCall] }` + `total_input/output/total`.

**ActionType** (`shared/schemas.py`) — catálogo cerrado de 31 valores:

```
REGISTER_SALE          REGISTER_CASH_INFLOW    REGISTER_EXPENSE
REGISTER_PURCHASE      REGISTER_CASH_OUTFLOW   UPDATE_STOCK
UPDATE_PRODUCT         REGISTER_STOCK_LOSS     CREATE_PURCHASE_SUGGESTION
IMPORT_TABULAR_FILE    PARSE_DOCUMENT_FILE     GENERATE_HEALTH_REPORT
ANSWER_HELP_REQUEST    CREATE_SUPPLIER_DRAFT   CLASSIFY_GMAIL_MESSAGE
SYNC_TO_GOOGLE         CREATE_CALENDAR_EVENT
# Stage 4 — Google writes via GoogleToolBroker
UPLOAD_TO_DRIVE        CREATE_GOOGLE_DOC       APPEND_TO_SHEET
# Sprint 17 — analíticos read-only (LOW risk, sin aprobación, los LLM no calculan)
ANALYZE_FILE           ANALYZE_PRICES          ANALYZE_STOCK_DATA
ANALYZE_SALES_DATA     ANALYZE_EXPENSE_DATA    ANALYZE_SUPPLIER_DATA
SIMULATE_SCENARIO
# Reclasificación de gastos vía chat (MEDIUM, requiere aprobación)
RECLASSIFY_EXPENSE
# v4 — analítico de marketing read-only (LOW)
ANALYZE_MARKETING_DATA
# v4 F5 — capa consultiva (LOW, read-only, los LLM narran pero no calculan)
ANSWER_DATA_QUERY
# v4 F6a — cobranza WhatsApp click-to-chat (MEDIUM, requiere aprobación)
PREPARE_WHATSAPP_MESSAGE
```

Agregar/quitar requiere actualizar `RiskEngine` y sus tests.

**Auto-ejecución:** `REGISTER_EXPENSE` sin confirmación (`_AUTO_EXECUTE_ACTION_TYPES` en `api/v1/agent.py`). Prefijo "Gasto registrado.".

**Deduplicación por fingerprint** (`operation_fingerprints`): SHA-256 de `{tenant_id}:{action_type}:{amount}:{date}` para `REGISTER_SALE/EXPENSE/PURCHASE`. Duplicado → `execution_status: "DUPLICATE"`.

**Rate limit:** 50 mensajes/día por tenant. Redis key: `rate:chat:{tenant_id}:{date}`. Compartido entre `/chat` y `/chat/stream`.

**RiskEngine** (`shared/risk_engine.py`) — determinístico, sin LLM. `HIGH`/`MEDIUM` requieren aprobación; `LOW` no.

**Retry** (`POST /agent/retry/{pending_id}`): solo `status=APPROVED`, `execution_status in (FAILED, REQUIRES_RECONNECT)`, `is_external=True`, máximo 1 reintento.

**`execution_status`:** `IN_PROGRESS` → `SUCCEEDED | FAILED | REQUIRES_RECONNECT`. `REQUIRES_RECONNECT` = `McpToolAuthError`; frontend guía a `/apps`.

**ContextBuilder** (`shared/context_builder.py`) — prioridades de contexto (en orden):
1. `historical_data` (400t) → `conversation_history` (1000t) → `recent_events` (800t) → `current_snapshot` (600t, siempre) → `uploaded_files` (400t) → `agent_memory` (300t) → `business_heuristics` (300t, siempre) → `intent_and_entities` (200t, siempre)

**HeuristicEngine** (`shared/heuristic_engine.py`): `get(business_type)` síncrono; `get_async(...)` aplica `BusinessHeuristicOverride` de la DB. `to_prompt_fragment()` genera valores numéricos para system prompts — nunca texto narrativo. Fallback a `kiosco_almacen`.

**AgentCEO — flujo:** `nlp_preprocessor` (Sprint 18, `agents/shared/nlp_preprocessor.py`) normaliza lunfardo/rioplatense (merca, birra, remarcar, guita, etc.) antes del LLM; spacy (`es_core_news_sm`) opcional con fallback a regex puro; las entidades NLP se inyectan como anotación pre-análisis en el prompt del CEO. Luego `classify_intent()` → LLM (max_tokens=800) → **33 intents** del `INTENT_CATALOG` en español rioplatense (incluye `intent_desconocido`, `reclasificar_gasto` → `agent_expense` → `RECLASSIFY_EXPENSE`, y los 4 de v4: `analizar_clientes`/`analizar_marketing` → familias analíticas, `consulta_libre` → `ANSWER_DATA_QUERY`, `recordar_por_whatsapp` → `PREPARE_WHATSAPP_MESSAGE`). `INTENT_CATALOG` es `dict[str, {desc, triggers}]` y el CEO construye el system prompt dinámicamente desde el catálogo. **Sprint 19 (consolidación):** las 8 familias analíticas son UN intent cada una; el sub-análisis va en la entidad `analysis_type` (best-effort). `build_plan()` traduce `(intent, analysis_type)` → el `_intent` legacy que el handler espera vía `_resolve_legacy_discriminator()` (default por familia si falta `analysis_type`); el mapeo vive en `_ANALYTIC_FAMILIES`. `_LEGACY_INTENT_ALIASES` resuelve keys granulares en vuelo (red de seguridad). Luego `INTENT_TO_ACTION_TYPE` + `INTENT_TO_AGENT` (determinísticos, en `ceo/team_plan_builder.py`) → `build_plan()` → `AgentTeamPlan` → `registry.get_sub_agent(name)`. Intent `actualizar_producto` → `agent_stock` → `UPDATE_PRODUCT` action.

**Rescate de intent (Sprint 17):** cuando el CEO devuelve `intent_desconocido`, ChatOrchestrator NO corta inmediatamente. Aplica dos capas determinísticas (sin LLM) en `_rescue_unknown_intent()`: (1) `DataIntentExtractor` sobre los adjuntos parseados → si hay datos importables, mapea a `analizar_precios`(`analysis_type=lista`)/`analizar_archivo`; (2) `intent_rescue.rescue_intent()` (en `shared/intent_rescue.py`, devuelve `tuple[intent, entities]` con `analysis_type`) — scoring semántico de verbo ambiguo + objeto de negocio + tipo de adjunto, con normalización de voseo/tildes y fuzzy matching (`rapidfuzz`, umbral 85) para typos. Solo si ambas fallan corta: `out_of_scope` (off-topic claro → mensaje de scope) o `pedir_aclaracion_negocio`/`pedir_aclaracion_sobre_archivo` (suena a negocio pero ambiguo → pide detalle). Constantes `_NO_AGENT_INTENTS`/`_NO_AGENT_MESSAGES` en `chat_orchestrator.py`. Para los 7 ActionTypes analíticos (+`ANSWER_HELP_REQUEST`), `build_plan()` inyecta el sub-análisis en `entities["_intent"]` (resuelto desde `analysis_type`) para que el handler distinga sub-análisis dentro de un mismo ActionType.

**Handlers analíticos (Sprint 17):** read-only, LOW risk, sin aprobación. Math determinística centralizada en `shared/analytics.py` (funciones puras: márgenes, días de stock, sobrestock, estrella/problemático, anomalías de gasto, punto de equilibrio). Los agentes cargan datos vía repos y arman un `message` determinístico — los LLM NUNCA calculan. Familias: AgentStock (`ANALYZE_PRICES`, `ANALYZE_STOCK_DATA`), AgentIncome (`ANALYZE_FILE`, `ANALYZE_SALES_DATA` + clientes stub), AgentExpense (`ANALYZE_EXPENSE_DATA`), AgentSupplier (`ANALYZE_SUPPLIER_DATA`), AgentHealth (`SIMULATE_SCENARIO`: proyección de caja vía ForecastService + what-if vía DeterministicFinance). Queries nuevas: `product_repository.get_products_with_margin`, `sale_repository.get_daily_velocity`/`get_sales_by_product`, `expense_repository.get_expense_stats_by_category`.

**Capa consultiva v4 (F5) — `ANSWER_DATA_QUERY`:** consulta libre en lenguaje natural (LOW, read-only). 7 dominios, cada uno resuelto por su agente: caja/ventas (AgentIncome), gastos (AgentExpense), stock (AgentStock), proveedores (AgentSupplier), clientes (AgentClient), marketing (AgentMarketing), salud (AgentHealth). Intent `consulta_libre`. Los agentes cargan datos vía repos + `shared/stats_engine.py` (numpy/numpy-financial) e inyectan estadística determinística en `structured_data["analisis"]`; la narrativa la genera `shared/data_query_narrator.py` — **el LLM narra pero nunca calcula** (F7 cableó el `stats_engine` a los 7 dominios). `stats_enrichment.py` arma el enriquecimiento; STOCK concentra sobre valor `units×cost` (no `margin_pct`, porque HHI requiere montos aditivos).

**Cobranza WhatsApp v4 (F6a) — `PREPARE_WHATSAPP_MESSAGE`:** LOCAL y MEDIUM (requiere aprobación). AgentClient determina el destinatario (cliente con mayor saldo si no se especifica), arma el cuerpo determinístico y emite `requires_approval`. La ejecución genera un link `wa.me` (click-to-chat, sin API de WhatsApp) en `PendingActionService`. Intent `recordar_por_whatsapp`.

**AgentTeamPlan / AgentTask** (`shared/schemas.py`): `AgentTeamPlan { plan_id, intent, tasks: list[AgentTask], requires_synthesis, fallback_message? }`. `AgentTask { task_id, agent, action_type, entities, depends_on: list[task_id], approval_group? }`. Stage 3 completado: `build_plan()` soporta planes multi-task y DAGs; ejecución con skip-downstream ante fallo; `agents/ceo/synthesis.py` genera narrativa sintetizada de múltiples resultados (`claude-sonnet-4-6` desde Sprint 18).

**GoogleToolBroker** (`agents/google/tool_broker.py`): ejecuta las 3 acciones Google de escritura (`UPLOAD_TO_DRIVE`, `CREATE_GOOGLE_DOC`, `APPEND_TO_SHEET`) via MCP. `PendingActionService` lo llama al confirmar acciones externas de AgentGoogle.

**Streaming SSE** (`POST /agent/chat/stream`): `{"type": "thinking"}` → `{"type": "response", "data": AgentResponse}` → `{"type": "error"}`. Finaliza con `data: [DONE]`. Frontend: `sendStream()` con placeholder `thinkingId` que se actualiza in-place.

**ChatOrchestrator** (`app/application/services/chat_orchestrator.py`): carga 4 capas de contexto (fail-silencioso): negocio + heurísticas, BusinessMemory, AgentMemory, últimos 5 UploadedFile. CEO clasifica → despacha sub-agente → si `requires_google_auth` retorna sin LLM → si `requires_approval` usa summary estructurado → **delega la generación de la respuesta a AgentChat** (Sprint 18; `_generate_rich_response`/`_format_agent_result`/`_render_session_memory`/`_format_history_turn` se movieron a AgentChat y se eliminaron del orchestrator). Acumula todas las `LLMCall` en `all_llm_calls` → `AgentResponse.usage`. Siempre inyecta `intent` + `target_agent` en `result`.

**ConversationService**: Redis caché caliente (TTL 24h) + PostgreSQL. Ventana deslizante 10 turnos. `conversation_id` = UUID generado en el cliente.

**EventBus** (`shared/event_bus.py`): wrapper sobre Celery `send_task`. Sin suscripción CEO en código — coordinación via tasks/event handlers.

**AgentHealth v2 — sub-pipeline interno** (`agents/health/`):
- `sub_collector.py` — recolecta `BusinessState` vía BSL (misma fuente que Celery)
- `sub_calculator.py` — calcula `ComponentScoresV2` (fórmula: `cash×0.30 + stock×0.20 + supplier×0.10 + margin×0.20 + growth×0.20`)
- `sub_narrator.py` — genera narrativa ejecutiva con `claude-sonnet-4-6`
- `scorer.py` — shim de compatibilidad con la API anterior

**AgentHelper — contrato `redirect_to`:** si el usuario pregunta algo del negocio (fuera del scope del manual), AgentHelper devuelve `result["redirect_to"] = "main_chat"`. Si AgentCEO detecta pregunta de ayuda de plataforma, devuelve `result["redirect_to"] = "help"`. Frontend `/help` usa `HelpChat.tsx`; endpoint: `POST /agent/help/chat` (sin rate limit, sin token billing de sesión).

**Extras:** `agents/supplier/preflight.py` — validaciones pre-envío Gmail.

### Sistema de Memoria (cuatro capas)

| Capa | Servicio | TTL | Qué almacena |
|------|----------|-----|--------------|
| Conversacional | `ConversationService` | 24h Redis + PG | Últimos 10 turnos |
| Negocio | `BusinessMemoryService` | 5min | Resumen financiero (ventas, gastos, alertas) |
| Agente | `AgentMemoryService` | 5min | Patrones: método de pago, monto promedio (Welford), top ActionTypes |
| Sesión chat | `ChatMemoryService` | permanente PG | Eventos de carga de datos (DATA_LOADED, DATA_REJECTED, FILE_UPLOADED, INTENT_DETECTED, QUERY_ANSWERED) |

`AgentMemoryService.record_action()` post-acción confirmada (fail-silent). Confianza: `min(1.0, 0.5 + count/20)`.
**Tabla:** `agent_memory` — `(tenant_id, key)` unique, `value` JSONB, `occurrence_count`, `confidence`.
**`ChatMemoryService`**: escribe en `chat_session_log` (migración `20260503_0002`). Permite saber qué se cargó en sesiones previas sin depender del historial conversacional.

### Integración MCP Google

Feature flag: `ENABLE_GOOGLE_MCP_TOOLS=false` (default). Variables propias: `GOOGLE_MCP_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI` — redirect al MCP server, no al backend.

**Port/adapter:**
- `app/application/ports/mcp_gateway.py` — interface `McpToolGateway`
- `app/integrations/mcp/http_gateway.py` — `HttpMcpGateway` (HTTP JSON-RPC 2.0)
- `app/integrations/mcp/google_mcp_service.py` — `GoogleMcpService` con allowlist por agente
- `app/integrations/mcp/exceptions.py` — `McpToolAuthError`

**Allowlists por agente:** frozensets de `google.gmail.*`, `google.calendar.*`, `google.sheets.*`, `google.docs.*`.

**Router `integrations`** (`app/api/v1/integrations.py`):
- `GET /integrations/google/status` — estado (`CONNECTING/CONNECTED/DISCONNECTED/ERROR`)
- `POST /integrations/google/connect/start` — inicia OAuth; crea `GoogleMcpConnection(status=CONNECTING)`
- `POST /integrations/google/disconnect` — revoca tokens

**ORM:** `GoogleMcpConnection` (`app/persistence/models/google_mcp_connection.py`) — `google_mcp_connections`, cols clave: `tenant_id`, `user_id`, `status`, `scopes_granted` (JSONB), `connected_at`.

**`google_oauth_tokens`** — gestionada por el MCP server directamente. Callback fallido → `last_error_code` en `mcp_server/app/auth/service.py`. `CONNECTING` > 10 min expira como `oauth_callback_timeout`.

**Prompt defense:** todo input de usuario a LLM pasa por `wrap_user_input()`. `is_valid_action_type()` valida output LLM contra catálogo cerrado.

### Archivos e ingestión

**`file_parsing.py`** centraliza: sanitización, detección MIME, `parsed_summary_json`, helpers chat (`summary_columns`, `summary_row_count`, `preview_value_from_summary`).

**Chat upload** (`POST /files/upload?purpose=chat`): parseo síncrono → `UploadedFile(processing_status=DONE)`. Fallo → 422, sin persistir.

**Ingestión** (`api/v1/ingestion.py` + `jobs/ingestion_worker.py`): Upload → Parse async (Celery) → Preview → Confirm (humano obligatorio).

### Observabilidad

- `structlog`: `from app.observability.logger import get_logger` → `get_logger(__name__)`.
- `bind_request_context(tenant_id, user_id)` en `deps.py` por request.
- Rate limiting: `slowapi` (200 req/min).

---

## Arquitectura del frontend

- Next.js 15 App Router. Protegidas: `src/app/(protected)/`. Públicas: `src/app/(public)/`.
- Estado global: Zustand (`src/stores/`). Server-state: TanStack Query.
- HTTP: axios wrapper `src/lib/api.ts`. UI: Tailwind CSS + `src/components/ui/`. Charts: Recharts. Forms: Zod.

### Estado visual

- Tema dark: `src/styles/globals.css` + `tailwind.config.ts` con tokens `vektor-*`.
- Tipografías en `src/app/layout.tsx`: `Barlow Condensed` (UI) + `Inter` (texto).
- `EconomicTicker` renderizado desde `(protected)/layout.tsx` solo en rutas dashboard; mobile → botón/modal.
- Dashboard: `DashboardLaunchpadNav` tabs/dots compartidos entre `/dashboard` y `/dashboard/analisis`.

### Organización del frontend

| Directorio | Responsabilidad |
|------------|-----------------|
| `src/features/` | auth, chat, dashboard, onboarding, ingestion, notifications |
| `src/services/` | HTTP por dominio: auth, sales, expenses, products, health_score, dashboard, momentum, notifications, ingestion, onboarding, files, agent |
| `src/stores/` | `authStore` (JWT + user), `toastStore` |
| `src/types/api.ts` | Tipos TypeScript de respuestas API |
| `src/components/auth/AuthHydrationBoundary.tsx` | Hidrata auth desde localStorage |

### Rutas protegidas (`src/app/(protected)/`)

| Ruta | Descripción |
|------|-------------|
| `/chat` | **Home principal** — chat página completa |
| `/dashboard` | Launchpad 1: health score hero + KPIs |
| `/dashboard/analisis` | Launchpad 2: charts + ForecastPanel + breakdown gastos + stock crítico |
| `/sales` | Analytics + lista de ventas |
| `/expenses` | Analytics + lista de gastos |
| `/products` | Catálogo; query param `?stock=ok|low|out` |
| `/apps` | Integraciones Google |
| `/settings` | Cuenta, configuración, panel de custom fields (`FieldDefinitionsPanel` + `SchemaERDView`), config de margen (`HealthConfigPanel`) y horarios laborales |
| `/help` | Chat de ayuda de plataforma — `HelpChat.tsx`, endpoint `/agent/help/chat` |
| `/ingestion` | Pipeline de ingestión de archivos con preview/confirm |
| `/otros` | Registros sin clasificar (`unclassified_records`) — clasificación manual + importación masiva de sugeridos |
| `/resumen-economico` | Resumen económico analítico (`GET /economic-summary`) — no contable, con disclaimers |

**Ruta pública:** `/oauth/callback?session_id=` → `POST /auth/oauth/google/exchange`.

### Chat

- `/chat` = home post-login. `ChatPanel.tsx` existe pero no está en el layout global.
- `conversation_id`: UUID generado por `useChatStore.newConversation()`. No viene del servidor.
- Adjuntos: `AttachmentPicker.tsx` — hasta 3 archivos, upload inmediato a `POST /files/upload?purpose=chat`.
- `useChat` expone `send` (REST) y `sendStream` (SSE). `MUTATING_ACTIONS`: 7 types que invalidan queries.

### Google OAuth login federado

1. `POST /auth/oauth/google/start` → redirect a Google
2. `/oauth/callback?session_id=` → `POST /auth/oauth/google/exchange`
3. Si `link_required`: vincular con contraseña → `POST /auth/oauth/google/link-pending`

### Integraciones MCP (frontend)

- `/apps` gestiona conexión, reconexión y desconexión Google.
- Si falla con `REQUIRES_RECONNECT` → guía a `/apps`.
- `GOOGLE_MCP_OAUTH_REDIRECT_URI` debe estar registrado exactamente en Google Cloud Console.
- Botón conexión abre ventana en blanco y redirige cuando llega `auth_url` (evita popup blocker).

### Endpoints frontend recientes

- `GET /api/economia` — dólar oficial/blue/MEP/CCL + inflación/REM/BCRA (`revalidate=1800`)
- `GET /forecast/cash` → `fetchCashForecast()` → `CashForecastResponse/ForecastPoint`
- `GET /insights/breakdown?days=N` → `BusinessBreakdownResponse`
- `GET /health-scores/history/v2` → `HealthScoreV2Response[]`
- `GET /insights/current` → `InsightBlock` en análisis (el stub `/api/analisis/insight` ya no se usa)

---

## Historial y migraciones

El historial de sprints (1–21), los proyectos mergeados post-Sprint-21 y la cadena completa de migraciones Alembic viven en **`docs/CHANGELOG-internal.md`** (se movieron fuera de este archivo para abaratar el costo de contexto por sesión).

**Email (referencia rápida):** Resend HTTP API vía `httpx` (`app/integrations/smtp.py`); Railway bloquea port 587. Vars: `RESEND_API_KEY` + `SMTP_FROM_EMAIL`.

---

## Reglas de trabajo

- **Mostrar plan antes de escribir código y esperar confirmación.**
- Tipos estrictos (`mypy strict=true`). Cada endpoint necesita schema Pydantic.
- `tenant_id` del JWT en CADA query de negocio — nunca del cliente.
- Scores recalculan solo ante cambios de datos (Celery async).
- Toda decisión → `decision_audit_log` (insert-only).
- Fail-closed en writes sensibles.
- `ActionType` cerrado (31 valores) — cambiar requiere actualizar `RiskEngine` y tests.
- System prompts: heurísticas como valores numéricos, nunca texto narrativo.
- Todo input de usuario a LLM pasa por `wrap_user_input()`.
- Toda aritmética financiera va por un servicio determinístico — LLMs nunca calculan montos. Métricas de negocio: `FactsService` (única fuente de verdad). Aritmética puntual del chat: `DeterministicFinance` (hasta migrar a FactsService).
- Custom fields no se validan en write time (MVP). Agregar validación en `field_definition_service.validate_custom_fields()` cuando sea necesario.
- **No-invention rule:** ningún componente del dashboard, agente LLM, ni job de background puede mostrar análisis, scores, narrativas, alertas o conclusiones cuando `confidence_level == "LOW"` (`data_completeness_score < 50`). La UI muestra un empty state solicitando los datos faltantes. Los jobs no persisten `Insight`, `ActionSuggestion` ni notificaciones analíticas con `confidence_level == "LOW"`. Nunca reemplazar scores `None`/`0` con defaults neutrales (`or 70`, `or 50`, etc.): si falta un componente, el score real es 0 o ausente — bajar la confianza, no maquillarlo.

---

## Tests

- pytest + pytest-asyncio (`asyncio_mode = "auto"`). DB en memoria: SQLite + aiosqlite.
- Cobertura: **50%** local, **60%** en CI.
- `pytest app/tests/domain/test_health_score.py -v --no-cov`

## CI

- `ci-backend.yml`: ruff + mypy + pytest (cov ≥ 60%) + Docker build. Triggers `backend/**` → `main/develop`.
- `ci-frontend.yml`: tsc + ESLint + `next build`. Triggers `frontend/**` → `main/develop`.

## Deploy (Railway + Vercel — beta)

| Servicio | Manifiesto | Start |
|----------|-----------|-------|
| `vektor-api` | `backend/railway.toml` | `sh scripts/start.sh` → uvicorn |
| `vektor-worker` | `backend/worker/railway.toml` | `sh scripts/start_worker.sh` → Celery |
| `vektor-mcp` | (servicio MCP propio) | — |
| Postgres | Neon (externo) | — |
| Redis | Railway managed | — |
| Frontend | Vercel, root `frontend/` | `next start` |

**Migraciones automáticas (Railway pre-deploy):** `backend/railway.toml` define `preDeployCommand = "sh scripts/migrate.sh"` (→ `alembic upgrade head`), que corre UNA vez por deploy del servicio `vektor-api`, en un contenedor one-off con `DATABASE_URL` de Neon, ANTES de que la nueva versión reciba tráfico. Si una migración falla, Railway aborta el deploy y la versión vieja sigue sirviendo (fail-safe). El `startCommand` (`start_web.sh`) ya NO corre Alembic — solo uvicorn. `make migrate-neon` queda para aplicar manualmente desde el shell (local contra Neon) cuando haga falta fuera de un deploy.

**Graceful bootstrap:** `app/bootstrap.py` captura errores DB/Redis → uvicorn arranca aunque estén caídos.
- `/health` — liveness, siempre 200 (usado por `healthcheckPath` en `railway.toml`, timeout 120s).
- `/ready` — chequea DB + Redis, devuelve 503 si falla (no usado por Railway actualmente).

**Beat** todavía no está desplegado como servicio (`backend/beat/railway.toml` en repo como referencia).

## Demo

`http://localhost:3000/demo` (password `Demo1234!`):

| Email | Vertical | Score |
|-------|----------|-------|
| demo.kiosco@vektor.app | Kiosco | 74 |
| demo.limpieza@vektor.app | Limpieza | 51 |
| demo.deco@vektor.app | Decoración | 62 |

8 semanas historial, 30 días transacciones, 8–15 productos. Regenerar: `make reset-demo`.
