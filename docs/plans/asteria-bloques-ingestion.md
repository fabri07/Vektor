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

## Bloque 7 — dry-run contra datos reales (SIGUIENTE)

Cierre integral, en DOS sesiones de proceso distintas (no alcanza correr dos
veces dentro del mismo proceso — no prueba que las decisiones persistidas se
recuperen en un preview futuro, solo que la memoria in-process seguía viva):

1. Conseguir/crear una base Postgres local o temporal vía `DATABASE_URL_LOCAL`.
2. Descargar el Excel real de Asteria desde R2 en modo lectura.
3. Habilitar los 3 flags de rollout SOLO para el tenant local que representa a
   Asteria (nunca Railway/producción):
   - `PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS`
   - `CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS`
   - `INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS`
4. Sesión de proceso A: preview + apply sobre la base local.
5. Sesión de proceso B (proceso nuevo, no el mismo intérprete): repetir
   exactamente la misma relectura y confirmar que el Bloque 5 recuerda lo que
   la sesión A confirmó.
6. Verificar que no cambien conteos ni aparezcan duplicados entre corridas.
7. Reportar resultados por hoja.
8. Dejar los runs históricos y producción sin modificar — nada de esto toca
   Railway ni Celery Beat (pendiente operativo aparte).

## Abierto / pendiente

- Confirmar con el usuario si existían Bloques 4 y 6 en el plan original (no
  se encontró rastro en código ni memoria).
- Bloque 7 sin ejecutar.
- Los 3 flags de rollout siguen en `[]` (nadie habilitado) hasta que el
  dry-run del Bloque 7 los valide.
