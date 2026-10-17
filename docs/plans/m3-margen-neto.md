# M·3 — margen neto como indicador de salud, margen bruto como desglose (4 PRs)

## Contexto
M·2b dejó una definición única del **margen bruto** (`domain/margen.medir_margen`, costo congelado por venta, cobertura ≥ 50%), pero en pantalla no la lee nadie: la tarjeta del dashboard promedia `margin_pct` del catálogo en React (`buildMarginBreakdown`, `frontend/src/features/dashboard/dashboardData.ts:205`), y la serie «Margen promedio» de Análisis calcula `(ventas − todos los gastos)/ventas` en React (`dashboardData.ts:547`). Además el **margen neto** tiene hoy dos definiciones que no coinciden:
- `FactsService.margen_neto` (`facts_service.py`, ~:540) = `(ventas − TODOS los gastos)/ventas`: resta las **compras de mercadería** del período en vez del costo de lo vendido (un mes de reposición fuerte se ve como desastre);
- el subscore del health score (`heuristics/health_engine.margin_net_ratio`, :212) = `(ingreso − costo vendido − gastos fijos recurrentes)/ingreso`, con fallback a los gastos declarados en el onboarding.

**Decisiones del dueño (2026-10-16):**
- El indicador de salud es el **margen neto**: `(ventas − costo de lo vendido − todo gasto vivo que no sea mercadería)/ventas` — alquiler, sueldos, servicios, comisiones, impuestos, fletes no capitalizados, recurrente o no.
- Se calcula por **mes calendario cerrado**; mientras corre el mes se ve el **margen bruto del mes en curso**. Tarjeta: «Margen neto · septiembre» arriba, «Margen bruto · octubre (en curso)» debajo, cada uno con su cobertura.
- **Una sola definición:** tarjeta, chat, alertas y subscore del health score leen el mismo `BusinessFact`.
- Los impuestos pagados llevan **tipo** (IIBB, monotributo, IVA, tasas municipales, otros) dentro de M·3. El IVA de un RI no es gasto (las ventas ya entran sin IVA): no resta del neto.

Ya resuelto y reutilizable: `medir_margen` (cobertura, estados, RI sin IVA), `gasto_de_resultado` (`persistence/repositories/_expense_scope.py`, el flete capitalizado no se cuenta dos veces), la categoría `TAXES` con alias (`domain/expense_categories.py:119,172-181`), el patrón de compuerta `config/*_rollout.py`, `Period` de FactsService, el universo de ventas sin `inflow`/anuladas/demo (las mixtas no se duplican: el margen sólo lee `sales_entries`).

---

## M·3-1 — una sola definición del margen neto (backend, sin pantalla)
**Cómo quedó (2026-10-17):** como abajo; detalle en `docs/claude/plataforma.md` (bullet M·3-1). La sombra va en `diag_margen.py` (read-only, no corre sola). La consulta de caja y el punto de equilibrio siguen con el bruto de 30 días.

1. **Dominio** — `domain/margen.py`: `medir_margen_neto(bruto: MedicionDeMargen, gastos_operativos: Decimal, *, gastos_observados: bool)` → `MedicionDeMargenNeto` (estado `confirmado|estimado|ausente`, motivo, `margen_pct`, `resultado_ars`, gastos). Ausente si el bruto es ausente (cobertura < 50%) o si no hay ningún gasto cargado en el mes (motivo «sin gastos cargados en el mes»: no se toma el gasto declarado del onboarding — no-invention). Hereda el estado y los supuestos del bruto (RI = estimado por la alícuota supuesta).
2. **Período** — `Period.mes_cerrado(today)` (mes calendario anterior completo) y `Period.mes_en_curso(today)` (1.º del mes → hoy), con `today_ar()`.
3. **FactsService** — `margen_neto` se redefine con `medir_margen_neto` sobre un mes cerrado: costo vendido = `MedicionDeMargen.costo_vendido`; gastos operativos = gastos vivos del período con `gasto_de_resultado` y **fuera** `expense_type == COGS` y `category in (INVENTORY, mercaderia)` (mismo criterio que `business_state_service.py:466`). `detail`: estado, motivo, ingreso, costo vendido, gastos por categoría, coberturas. La severidad del benchmark sigue sobre el neto. `ventas − todos los gastos` deja de llamarse margen (queda como flujo en `deterministic_finance`, que ya lo rotula «Resultado»).
4. **Health score** — `margin_net_ratio` pasa a leer el neto del **último mes cerrado** desde `leer_margenes` (sin fallback al onboarding; ausente → `score_margin=None` y baja la confianza un nivel como hoy, nunca LOW por sí solo). Detrás de compuerta `config/net_margin_rollout.py` (`NET_MARGIN_ROLLOUT_TENANT_IDS`): sin habilitar, el score sigue con la fórmula actual. Alertas `MARGIN_LOW` e insights (`insight_templates.py:147`) leen el mismo valor.
5. **Chat** — `margenes_del_tenant` devuelve neto del mes cerrado + bruto del mes en curso (+ bruto 30 días para lo que ya lo usa); `margen_texto` los dice con su mes y estado.
6. **Sombra** — `scripts/diag_margen.py` (read-only) suma por cuenta: neto actual de FactsService, neto del score actual, neto nuevo del último mes cerrado y subscore antes/después. Se corre local contra un dump/demo; el informe de Neon lo corre el dueño antes de habilitar cuentas.
7. Doc: `docs/claude/plataforma.md` (FactsService), `docs/plans/cierre-ingesta-capital-margen.md` §6 reescrito con estas decisiones; CLAUDE.md, línea de 2c si cambia la definición canónica.

**Tests:** dominio (neto con bruto confirmado/estimado/ausente, sin gastos → ausente, gastos no recurrentes cuentan, mercadería no cuenta, flete capitalizado no cuenta); `Period` (enero → diciembre del año anterior, mes de 28/29/31 días, hoy = día 1); FactsService con FakeProvider; reconciliación chat = score = fact contra la base; compuerta del score (mutación: sacarla cambia el score de una cuenta no habilitada); `test_facts_reconciliation` actualizado donde asume `ventas − todos los gastos`.

## M·3-2 — pantalla: tarjeta, Análisis, Productos y Ventas
**Cómo quedó (2026-10-17):** como abajo; la compuerta LOW usa el `data_completeness_score` del último snapshot del score. Detalle en `docs/claude/plataforma.md` (bullet M·3-2).
1. **Endpoint** `GET /insights/margins?from_date&to_date` (schema Pydantic `MarginsResponse`, `require_active_subscription_for_insights`, `tenant_id` del JWT): neto del último mes cerrado, bruto del mes en curso, bruto del período pedido, serie de bruto por bucket y de **neto sólo mensual** (meses cerrados), desglose del bruto por categoría de producto (cada categoría con su cobertura; ventas sin producto en «Sin producto»). El provider suma `product_id`/categoría al frame de ventas. Con `confidence == LOW` → vacío (no-invention).
2. **Dashboard** — `DashboardSummaryCards`: tarjeta como la maqueta aprobada, cada número con estado (estimado/sin medir + motivo) y cobertura. Se borra `buildMarginBreakdown` y su tooltip.
3. **Análisis** — «Margen promedio» se reemplaza por «Margen bruto» (del endpoint, por bucket) y «Margen neto» (mensual); se borra la fórmula de `buildLineSeries` para margen. Sin fórmulas de margen en React.
4. **Productos** — columna «Margen» visible por defecto, con motivo cuando no hay; `ProductResponse.margin_pct` con costo 0 → `null` + motivo «costo 0» (hoy da 100%).
5. **Ventas** — `/sales`: costo y margen por operación sólo para OWNER/ADMIN: campos opcionales en `SaleEntryResponse` llenos sólo para esos roles (los demás reciben `null`; el cajero ya tiene 403 en `/sales` y `PosProductResponse` no cambia). Columnas ocultables en `sales/page.tsx`.
6. Doc en el mismo commit (`docs/claude/plataforma.md`, `pos.md` si toca permisos).

**Tests:** endpoint (bordes de cobertura 49,99/50/50,01, mixta no duplica, anuladas/demo fuera, LOW vacío, aislamiento de tenant, ANALYST/VIEWER reciben `null` en costo de ventas, CASHIER 403); jest de la tarjeta (neto/bruto/ausente/estimado), de la serie y de la columna de ventas por rol; `margin_pct` con costo 0.

## M·3-3 — tipo de impuesto
1. Migración aditiva `expense_entries.tax_kind` (`iibb|monotributo|iva|tasa_municipal|otro`, NULL, CHECK; idempotente bajo advisory lock).
2. Escritores: `POST/PATCH /expenses`, carga manual, chat (`REGISTER_EXPENSE`), import (los alias ya existentes `iibb`, `monotributo`, `abl`, `municipal`, `tasas` fijan el tipo; `TAXES` sin tipo queda NULL). Formulario de gasto: selector de tipo cuando la categoría es Impuestos.
3. Neto: para un RI, `tax_kind = 'iva'` no resta (el ingreso ya va sin IVA); `TAXES` sin tipo resta igual y el `detail` lo avisa («N gastos de impuestos sin tipo»).
4. Doc: `docs/claude/plataforma.md` (categorías) e `ingesta.md` (alias).

**Tests:** migración (`-m postgres`), cada escritor, import por alias, neto RI con IVA excluido y monotributo con cuota incluida, aviso de sin tipo.

**Cómo quedó (M·3-3):** como el plan, más: el tipo se infiere sin fuzzy y sólo de la categoría cruda (nunca de la descripción); sin alias de categoría de IVA ni «seguridad e higiene» (falsos positivos en la revisión); un `iva` fuera de `TAXES` no excluye nada. Detalle en `docs/claude/plataforma.md`.

## M·3-4 — antigüedad del costo («costo desactualizado» > 45 días)
1. Migración aditiva `sales_entries.cost_source_date` (DATE NULL). `costeo_de_ventas` la escribe desde `CostoDeReferencia.fecha` (compra/stock inicial: fecha de negocio del movimiento; manual/catálogo: fecha de la edición; `costo_cargado` sin fecha → NULL = «antigüedad desconocida»).
2. Backfill de ventas viejas en `scripts/costeo_retroactivo_ventas.py --solo-fecha`: re-elige la fuente a la fecha de la venta y escribe la fecha **sólo si su costo coincide con el `cost_unit_ars` congelado** (nunca revalúa). Dry-run por tenant; **el `--apply` lo autoriza el dueño**.
3. Política `COSTO_DESACTUALIZADO_DIAS = 45` en `domain/margen.py` (bordes 44/45/46: > 45 desactualizado). El bruto informa importe vendido con costo desactualizado y su %; la tarjeta y la columna de Productos dicen «última compra hace X días». No se ajusta ningún costo por inflación.
4. Doc en el mismo commit.

**Tests:** costeo escribe la fecha por cada origen; backfill no toca ventas cuyo costo no coincide; bordes 44/45/46; endpoint y tarjeta con el rótulo.

---

**Cómo quedó (M·3-4):** como el plan. La antigüedad del margen es a la fecha de la venta; la de Productos, a hoy (sello del costo vigente). El backfill exige mismo costo **y** mismo origen. Detalle en `docs/claude/plataforma.md`.


## Verificación (cada PR)
- Backend con env de CI: `ruff check .`, `mypy --no-incremental app` (el mypy con caché no vio un error en M·P4-2), suite completa `-n 6`; `-m postgres -n 0` contra `vektor_pgtest` en los PRs con migración (M·3-3, M·3-4).
- Frontend (M·3-2): `npm run type-check`, `lint`, `npm run test -- --runInBand`.
- Ensayo en tenant demo: mes cerrado con ventas, compras y alquiler → la tarjeta da el neto esperado a mano, el chat dice el mismo número y el score (habilitado) también; sin gastos en el mes → «sin medir».
- Revisión adversarial (1 revisor), `git diff --stat`/`--check`, PR, CI verde, merge con `--match-head-commit`.

## Del lado del dueño
- Correr la sombra de M·3-1 en Neon y habilitar cuentas en `NET_MARGIN_ROLLOUT_TENANT_IDS`.
- `--apply` del backfill de fechas (M·3-4).
- Siguen pendientes: `backfill_costo_promedio.py --all --apply`, `AVG_COST_DISPLAY_ROLLOUT_TENANT_IDS`, cron `--pendientes`.

## Después
M·P4b (promedio neto de IVA para RI), D·3 y D·5.
