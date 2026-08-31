# Bloques Asteria — mapeo de columnas, costo, categoría y memoria de esquema

## Contexto

Diagnóstico read-only contra el tenant real de Asteria (`agustinalahora4@gmail.com`,
`backend/scripts/diag_asteria_tienda_readonly.py`, 2026-08-30) encontró varios
problemas concretos en el pipeline de ingestión que no tenían fase asignada en
`docs/plans/ingestion-mapping-overhaul.md`. Se armó un plan numerado por
bloques, ejecutado directo sobre `main` (no es la rama `feat/ingestion-identity-
reread-safety` / PR #48 — ver memoria `project_ingestion_column_mapping_ux_overhaul`,
que es un plan distinto y sigue sin mergear).

Este documento se escribió DESPUÉS de que la numeración original de bloques ya
existiera en el código (comentarios `# Bloque N` en el diff) — un `/clear` de
sesión se llevó puesta la conversación donde se armó el plan completo, y no
había quedado persistido en ningún archivo. Lo de abajo es la reconstrucción
verificada contra el código + lo que el usuario confirmó en el momento.

**Bloques 4 y 6 no aparecen en ningún lado del código ni de la conversación
recuperada.** Si existen en el plan original, quedan como pendiente de
confirmar con el usuario — no se inventó contenido para ellos acá.

## Estado por bloque

### Bloque 1 — hojas derivadas/resumen excluidas de "Otros"

`file_parsing.py`: una hoja resumen (Ganancias, balance por medio de pago,
etc. — el parser la marca `is_summary_or_derived: True`) ya NO cae a
`otros_detectados` (generaría `UnclassifiedRecord` y, peor, se sumaría a los
totales reales una segunda vez). Bucket propio `derived_detected`: preservada
completa para preview, pero excluida del cómputo por default. Si el usuario
la incluye a mano y le asigna una entidad, `ingestion_import_service` la
materializa desde ese bucket (`_bucket_key_for_context`).

Frontend: `SheetNavigator`/`FileInterpretationReview` muestran la hoja como
"Derivada — Véktor la calcula sola" con la explicación inline.

**Estado: implementado, con tests (`test_hoja_derivada_no_reasignada_no_contamina_otros`,
`test_hoja_derivada_reasignada_se_materializa` en `test_ingestion.py`).**

### Bloque 2 — "Tienda" → proveedor (no siempre marca)

Bug real: dos filas del mismo producto con distinta "Tienda" colapsaban en un
solo producto porque "Tienda" entraba a la identidad como marca. Ahora el
usuario puede mapear la columna a `supplier:name` (cross-entity target nuevo
en `CROSS_ENTITY_TARGETS["product"]`) y Véktor crea/reusa el vínculo
`ProductSupplierLink` (tabla nueva, migración `20260831_0001`) en vez de
tratarla como marca. Gateado por `PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS`
(vacío = comportamiento de hoy, "Tienda" sigue siendo `custom_fields["marca"]`).

**Estado: implementado, con tests (`test_product_supplier_links_bloque2.py`).**

### Bloque 3A — "compra+envío" como costo final del catálogo

La hoja "precios y stock" de Asteria trae "Precio de compra" (costo base) y,
para algunos productos, "compra+envío" (costo final ya calculado por el
proveedor). Cuando ambas columnas existen, "compra+envío" gana como
`unit_cost_ars` — el motor de flete de compras (F-H6) nunca corrió sobre
catálogo, así que no hay riesgo de sumar el envío dos veces. Los dos
auxiliares (`purchase_base_cost`, `shipping_percentage`) se preservan en
`custom_fields`, nunca se pierden. Gateado por
`CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS`.

**Estado: implementado, con tests (`test_catalog_final_cost_bloque3a.py`).**

### Bloque 3B — sugerencia de categoría de producto

Inferencia pura en `app/domain/product_category_inference.py` (nombre +
especificaciones de texto → categoría candidata + confianza). Wiring en
`ingestion_import_service`: confianza alta se aplica a `product.category`,
media queda solo como sugerencia (no se aplica), y una categoría ya
confirmada (a mano o por una relectura anterior) nunca se pisa — mismo guard
que ya protegía `existing.category`. Sin flag de rollout propio (deliberado:
es aditivo, nunca reemplaza una categoría ya puesta).

**Estado: implementado, con tests (`test_product_category_inference.py` +
`test_product_category_suggestion_bloque3b.py`).**

### Bloque 5 — persistencia y RECUERDO de decisiones por huella de esquema

Dos mitades, las dos completas:

**Persistencia** (`ingestion_schema_decision_service.py`, tabla
`ingestion_schema_decisions`, migración `20260831_0002`): cada confirm/
relectura con decisiones EXPLÍCITAS (mapeo columna→campo — incluye "Tienda"→
proveedor/marca/custom/ignorar, entidad de la hoja, inclusión, tratamiento de
stock, decisión de envío) graba una fila por `(tenant_id, schema_fingerprint,
context_signature, decision_type)`. Huella insensible al orden de columnas,
sensible a que el SET cambie (`app/domain/ingestion_schema_fingerprint.py`).
Nunca graba sugerencias automáticas ni exclusiones default — solo lo que el
caller pasó explícito. `format_version` permite invalidar el FORMATO del
payload a futuro sin migración.

**Consumo** (agregado en esta misma pasada, era el hueco que faltaba):
`lookup_remembered_decisions_for_contexts()` se llama desde:
- `GET /ingestion/files/{id}/preview` (`FilePreviewResponse.remembered_decisions`,
  por `context_id`).
- `build_reread_sheets` (`RereadSheetStatus.remembered_decisions` por hoja),
  consumido por `POST /files/{id}/reread/preview`.

Ambos son de solo LECTURA — nunca escriben, nunca cambian `status`/
`columns_mapped`/`entity_type` de la hoja por sí solos. Frontend
(`FileInterpretationReview.tsx`): al abrir una hoja por primera vez, precarga
el borrador editable (mapeo, entidad, inclusión, stock) con lo recordado,
muestra un banner "Precargamos el mapeo y la sección de una carga anterior..."
y cada columna recordada se etiqueta "Recordado de una carga anterior con
este mismo formato" (`mappingRules.ts` — distinto de `tenant_history`, que es
un alias aprendido y difuso, no la misma decisión exacta). Todo sigue editable
antes de "Actualizar vista previa" / aplicar — nada se manda al backend hasta
que el usuario dispara esa acción. La decisión de ENVÍO recordada se muestra
como nota informativa (no hay control editable para envío en este panel
—vive en `ColumnMapperPanel`, el flujo de carga inicial— así que no se
pretende que sea editable ahí donde no lo es).

Gateado por `INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS` (vacío = ni
escribe ni lee, comportamiento de hoy).

**Estado: completo (persistencia + consumo), con tests de integración:**
- Backend: `test_ingestion_schema_decisions_bloque5.py` (16 tests: escritura +
  consumo — segunda sesión recupera, Tienda→proveedor en el preview,
  inclusión/stock/envío precargados, corrección reemplaza lo recordado, flag
  apagado no precarga nada, cambio de esquema no precarga nada, no se aplica
  silenciosamente + no escribe en un GET) + `test_ingestion_schema_fingerprint.py`
  (dominio) + 2 tests HTTP en `test_ingestion.py::TestPreviewEndpoint`.
- Frontend: `file_interpretation_review.test.tsx` (2 tests nuevos: precarga +
  editable, y "no se aplica silenciosamente" — el POST a `reread/preview`
  solo sale cuando el usuario hace click).

## Bloque 7 — dry-run contra datos reales (COMPLETO, 2026-08-31)

Script: `backend/scripts/asteria_dryrun_bloque7.py`. Tenant + `uploaded_file_id`
determinísticos (UUID fijo), las 3 flags de rollout se habilitan SOLO para ese
tenant DENTRO del propio script (antes de importar `Settings`) — nunca toca
Railway/producción; guarda de seguridad (`_abort_if_prod_like`) que aborta si
`DATABASE_URL` contiene un host administrado (Neon/Railway/RDS/Supabase).

Infra: Postgres descartable en Docker (`vektor-asteria-dryrun`, puerto 55432,
ya destruido al cerrar) + credenciales R2 read-only en un archivo temporal del
scratchpad de la sesión (ya borrado). `alembic upgrade head` corrió limpio de
punta a punta contra una base nueva — valida también la cadena completa de
migraciones en Postgres real, lo mismo que el paso de CI.

**Sesión A** (proceso 1): descargó `ASTERIA_home_deco.xlsx` real desde R2
(277.741 bytes, 9 hojas), armó el mapeo vía el motor de sugerencias real de
la app (`ColumnMappingService.suggest_mappings`, sin LLM — mismo camino que
usa el preview) + la corrección real de Bloque 2 ("Tienda"→`supplier:name`),
y confirmó contra la base local. `remembered_decisions` dio `(vacío)` antes de
confirmar, como corresponde a una primera sesión.

**Hallazgo real (antes del fix de abajo):** Bloque 3A no se disparaba nunca
en un alta real — ver sección siguiente.

**Sesión B** (proceso 2, intérprete nuevo, mismo Postgres): repitió la MISMA
relectura. Antes de confirmar nada, `lookup_remembered_decisions_for_contexts`
recuperó las 5 hojas que la Sesión A había confirmado — incluido
`Tienda → {'Tienda': 'supplier:name'}` — probando que Bloque 5 persiste
across procesos, no solo dentro de la misma sesión in-process. Tras
re-confirmar, conteos IDÉNTICOS a la Sesión A en las 6 tablas verificadas
(`sales_entries` 1939, `expense_entries` 624, `products` 397,
`product_supplier_links` 238, `ingestion_schema_decisions` 10,
`unclassified_records` 9) — cero duplicados.

### Fix aplicado — Bloque 3A no se disparaba en un alta real

**Causa:** `_uc_mapped = cols.get("unit_cost_ars")` en `_add_product`
(`ingestion_import_service.py`) ganaba INCONDICIONALMENTE sobre la detección
de "compra+envío". El frontend (`ColumnMapperPanel.tsx`, tanto el camino
multi-hoja como el plano) precarga el mapeo con TODAS las sugerencias,
tocadas o no — y la sugerencia heurística de "Precio de compra" es
`unit_cost_ars` (keyword "compra"). Verificado contra productos reales del
Excel: `unit_cost_ars` quedaba en el costo base, nunca en compra+envío, con
el flag prendido.

**Fix:** `_es_costo_base_ambiguo()` — si la columna mapeada a `unit_cost_ars`
es la MISMA columna ambigua de costo base ("Precio de compra" y variantes,
detectada por el mismo criterio que ya usa Bloque 3A) y no es ya la propia
columna final, no cuenta como elección deliberada: se deja pasar a la
detección de "compra+envío". Una columna DISTINTA (ej. "costo_real",
`test_mapeo_manual_gana`) sigue ganando sin más — sin cambios de
comportamiento ahí, y ninguno con el flag apagado.

Tests nuevos: `test_sugerencia_default_de_precio_de_compra_no_bloquea_compra_mas_envio`
+ `test_sugerencia_default_sin_flag_mantiene_comportamiento_previo`
(`test_catalog_final_cost_bloque3a.py`, 9/9 en verde). Verificado además
contra los productos reales de Asteria tras una base limpia: `unit_cost_ars`
coincide exacto con `compra+envío` en los 8 productos muestreados, y sigue
correcto después de la Sesión B (no se revierte en una relectura). Suite de
ingestión completa (1005 tests) sin regresiones.

## Bloque 4 y Bloque 6 (aclarados por el usuario, 2026-08-31)

**Bloque 4** — contraste, navegación completa y humanización de `col_8` —
sí existía en el plan original. Ya está en `main` (`f159e979`): es el trabajo
marcado como "F10-fix contraste" en el diff de `DataSample.tsx`/
`FileInterpretationReview.tsx`/`SheetNavigator.tsx` + `SheetNavigator`
Anterior/Siguiente + `frontend/src/lib/columnLabels.ts` (`humanizeColumnLabel`,
"Columna sin encabezado N" en vez de `col_8` crudo).

**Bloque 6** — idempotencia y reemplazo seguro de la lectura anterior. No era
una implementación nueva: la mayor parte ya vivía en `main` (motor de
void/preserve/reimport de `reread_service.py`, mucho antes de este plan).
Se pidió validar 6 casos puntuales contra tests existentes:

| # | Caso | Estado |
|---|---|---|
| 1 | Preservación de `has_user_edits` | ✅ cubierto — `test_reread_file.py::test_reread_preserves_edited_and_reimports_others` |
| 2 | Rollback íntegro si falla el apply | ✅ cubierto en el confirm (`test_ingestion.py::test_rollback_integral_si_confirm_falla` + `test_failure_after_f5_savepoints_still_compensates_lease`); `apply_reread` comparte el mismo savepoint pero sin test dedicado propio — gap menor, riesgo bajo (código ya ejercido) |
| 3 | Eliminar resultados de una hoja ahora excluida | ❌ **no era solo un gap de test — bug real**, ver abajo |
| 4 | Reversión acotada de `product_supplier_links` | ✅ cubierto — `test_product_supplier_links_bloque2.py::test_exclusion_posterior_revierte_solo_el_vinculo_atribuible` |
| 5 | Conservación de vínculos `purchase_evidence` | ✅ cubierto — `test_purchase_evidence_no_se_elimina_al_retirar_declaracion_de_catalogo` |
| 6 | Sin referencias activas duplicadas por archivo | ✅ cubierto — `test_relectura_identica_es_idempotente` |

### Bug real encontrado y arreglado — caso 3

`apply_reread` (`reread_service.py::_reconcile`) llamaba a
`insert_confirmed_data` sin pasarle `context_confirmed` del borrador —
**solo** `build_reread_sheets` (el cálculo del PREVIEW) lo usaba, para el
status que se muestra en pantalla. Consecuencia real: un usuario que
desmarca "Incluir esta hoja en la relectura", ve la exclusión reflejada
correctamente en el preview, y al aplicar — esa hoja se **reimporta
igual**. `git blame` confirma que es preexistente (junio–agosto 2026, ajeno
a este plan). Verificado con un test que reproduce el caso end-to-end antes
del fix: `voided=3, new=0`, pero terminaban 3 filas activas (la de la hoja
"excluida" incluida).

**Fix:** extraer `context_confirmed` del `draft` en `_reconcile` (mismo
patrón que `_draft_effective_mappings` ya hace con `column_mappings`/
`context_entities`) y pasarlo a `insert_confirmed_data`. `None` sin borrador
(cae al criterio de siempre, sin cambios de comportamiento cuando nadie usó
el checkbox de inclusión). Test nuevo:
`test_reread_file.py::test_reread_excluye_hoja_elimina_sus_filas_previas`
(XLSX real de 2 hojas, exclusión explícita de una vía `draft.context_confirmed`).
Suite completa de reread (68 tests) + suite de ingestión (1006 tests) sin
regresiones. Código en producción desde antes de este plan — este fix SÍ
afecta comportamiento ya en uso (a diferencia de los Bloques 1–5/7, todos
detrás de flags apagados).

## Abierto / pendiente

- Los 3 flags de rollout siguen en `[]` en producción (nadie habilitado) —
  el dry-run del Bloque 7 los validó localmente, falta la habilitación
  controlada real (fuera de alcance de este plan; decisión operativa aparte).
- `historial_sin_fecha: 538` en la Sesión A (más de la mitad de algo, sobre
  filas con fecha no parseable, correctamente NO inventada como "hoy" por F6)
  — no se investigó a fondo, podría valer la pena revisarlo con el negocio
  real antes de una habilitación productiva.
- Caso 2 de Bloque 6 (rollback dedicado de `apply_reread`) sin test propio —
  gap menor, no se agregó test (fuera de lo que el usuario pidió cerrar).
