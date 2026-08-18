# F-S.0 · Catálogo y transacciones se vinculan en la misma carga — Implementation Plan

> **ESTADO: ✅ ENTREGADO (2026-08-14).** Las 4 tareas de abajo son el borrador PRE-revisión —
> quedan como referencia del proceso TDD, pero el código real diverge de varios pasos acá
> descriptos porque una revisión encontró 5 bloqueantes reales antes de ejecutar la Tarea 4 (y
> un bug de ambigüedad en la Tarea 3). El resumen autoritativo de qué se entregó y en qué
> difiere de este borrador vive en `docs/plans/ingestion-mapping-overhaul.md`, sección
> `# F-S.0` (bloque "✅ ENTREGADO" al principio de la sección). Commits reales:
> `2191dbe5` (Tarea 1, incluye `RESOLUCION["sale"]` que este borrador no contemplaba),
> `4063b33c` (Tarea 2, tal cual), `936e3c9d` (Tarea 3, con la ambigüedad corregida y
> validación de datos legacy que este borrador no tenía) y el commit de la Tarea 4
> (reescrita: orden de rutas, `has_user_edits`, auditoría agrupada, `.delay()`, guard de
> mantenimiento, paginación real, candidatos, warning honesto — ninguno de estos 8 puntos
> estaba en el borrador original de abajo). Para tocar este código de nuevo, leer el código
> y sus tests reales primero — no re-ejecutar los pasos de abajo tal cual.
>
> **Segunda pasada — `/code-review high` sobre el diff final** encontró 3 hallazgos reales más,
> ya corregidos: (1) el conteo de `ventas_sin_producto` sólo miraba la columna de NOMBRE — una
> hoja que mapea únicamente `sku`/`barcode` (el caso que la Tarea 1 habilita) y no resuelve
> quedaba invisible, sin contar y sin entrar a la cola; se amplió a nombre O sku O barcode, el
> que haya venido. (2) el POST de vinculación calculaba `truncated` en el escaneo pero lo
> descartaba al responder — un grupo más grande que el tope se vinculaba parcialmente con
> `linked: N` sin avisar que quedaba resto; se agregó `truncated` a la respuesta. (3) la lógica
> de `truncated` original asumía "llegué al tope ⇒ hay más", lo cual es falso cuando el tope
> coincide justo con el final de los datos (probado por un test que lo hacía fallar) — se
> corrigió a seguir escaneando SIN agregar más a `matches` hasta comprobar si de verdad queda
> algo, no asumirlo. Deuda declarada (no corregida, documentada en el rector): el aprendizaje de
> alias sólo se escribe desde la cola nueva, no desde la rama equivalente de
> `others.py::reclassify_record`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Una venta importada vincula contra el catálogo por código (sku/barcode) además de por nombre, ese vínculo funciona aunque el catálogo llegue en el MISMO archivo, y una venta que igual queda sin producto se cuenta, se conserva su nombre crudo y se puede resolver en bloque desde una cola agrupada — nunca en silencio.

**Architecture:** Backend-only, aditivo, sin migraciones. Reusa infraestructura F2/F-H1 ya construida (motor de identidad, índices en memoria por corrida) en vez de escribir un segundo motor. Cuatro mecanismos independientes y commiteables por separado:
1. `sku`/`barcode` como target explícito de `sale` (hoy solo `expense` los tiene).
2. Cerrar un gap real en el índice transaccional: un producto creado desde catálogo en la MISMA corrida no queda indexado por barcode para que una venta posterior lo encuentre (sku/nombre sí funcionan desde F-H1; barcode no).
3. Alias persistido: cuando el usuario vincula a mano un nombre de archivo a un producto, ese nombre queda guardado para que la PRÓXIMA importación resuelva sola.
4. Ventas sin producto: se cuentan, se avisa en el confirm, y una cola nueva (agrupada por nombre) permite vincularlas en bloque — reusando el alias de la tarea 3.

**Tech Stack:** FastAPI + SQLAlchemy async + pytest-asyncio (SQLite en memoria en tests). Sin librerías nuevas.

**Spec:** `docs/plans/ingestion-mapping-overhaul.md`, sección `# F-S.0 · Catálogo y transacciones se vinculan en la MISMA carga` (líneas 1184-1216 al momento de escribir este plan). Contexto de por qué F-S.0 es bloqueante: `# Actualización 2026-08-14: prueba real de ASTERIA en prod`, mismo archivo.

## Global Constraints

- **Sin migraciones.** Todo vive en `sales_entries.custom_fields` (JSONB existente) y `products.custom_fields` (ídem). Ver invariante del plan rector: "El programa F-0 → F-E era aditivo y sin migraciones" (F-I es la única excepción, no aplica acá).
- **No-invention.** Sin código y sin nombre suficiente, Véktor NO adivina — la venta queda sin producto, CONTADA y visible en la cola. Nunca se asigna un producto "parecido" automáticamente.
- **`tenant_id` sale del JWT** en cada query nueva — nunca del body/path (`get_current_tenant`).
- **Endpoints nuevos que mutan datos van gateados con `require_modify_access`** (PIN step-up), igual que el `PATCH /sales/{id}` existente. El `GET` de la cola no lleva PIN (es lectura).
- **`custom_fields` se reasigna, nunca se muta in-place**: `obj.custom_fields = {**(obj.custom_fields or {}), "key": value}` — es el único patrón que SQLAlchemy detecta como cambio en esta columna (no hay `MutableDict`, verificado: `products.py:88` es un `mapped_column` simple).
- **No usar predicados SQL sobre claves de `custom_fields` (JSONB/JSON).** No hay precedente en el código de filtrar por `custom_fields->>'x'` en una query, y SQLite (tests) vs Postgres (prod) difieren en soporte de JSON — ver `[[feedback_sqlite_masks_postgres]]` en la memoria del proyecto: un test verde en SQLite no prueba nada sobre Postgres si el camino que ejercita no es el mismo. Las tareas 4 filtran por `product_id IS NULL` en SQL (columna real, sin ambigüedad de dialecto) y por el nombre crudo en Python, sobre un conjunto ya acotado.
- **Responder y commitear en español.** Un commit por tarea, con `Co-Authored-By: claude-flow <ruv@ruv.net>`.
- **NO correr `ruff format` / `make format`** — el backend no está normalizado; usar `make fix` si hace falta autofix de lint y revisar el diff.
- Verificación por tarea: `cd backend && .venv/bin/python -m pytest <archivo> --no-cov -q`, más `ruff check` y `mypy` sobre los archivos tocados antes de cada commit.

---

### Task 1: `sku`/`barcode` como target explícito de `sale`

**Contexto medido (no se repite investigación, se declara):** el motor de resolución YA lee `cols.get("sku")`/`cols.get("barcode")` para vincular una venta — `_venta_producto_id` (`app/application/services/ingestion_import_service.py:5169-5178`, path multi-hoja) y la resolución del path plano (`ingestion_import_service.py:3876-3884`) ya llaman a `_resolve_product(..., by_barcode=_identity_indexes.by_barcode, barcode=...)`. Lo que falta es que el usuario pueda MAPEAR una columna a esos targets: `CANONICAL_FIELDS["sale"]` (`app/application/services/column_mapping_service.py:35-55`) no los declara, así que `GET /ingestion/field-catalog` nunca los ofrece para una hoja de ventas y el `<select>` del panel no tiene esa opción (`expense` sí la tiene, líneas 77-78 del mismo dict). `parse_target()` (`column_mapping_service.py:1319-1342`) es agnóstico de entidad — no valida contra `CANONICAL_FIELDS`, así que agregar las dos claves no requiere tocar el parser ni el resolvedor, solo declarar el target para que el catálogo lo sirva.

**Files:**
- Modify: `backend/app/application/services/column_mapping_service.py:35-55` (dict `CANONICAL_FIELDS["sale"]`)
- Test: `backend/app/tests/api/v1/test_field_catalog_sale_sku_fs0.py` (create)
- Test: `backend/app/tests/services/test_ingestion_sale_link_by_code_fs0.py` (create)

**Interfaces:**
- Consumes: `CANONICAL_FIELDS: dict[str, dict[str, str]]` (global existente, `column_mapping_service.py:34`); `insert_confirmed_data(session, tenant_id, summary, confirmed_fields=None, context_mappings=None, context_confirmed=None) -> dict[str, Any]` (`ingestion_import_service.py:2963`, firma completa ya vigente — no cambia en esta tarea).
- Produces: `CANONICAL_FIELDS["sale"]` con dos entradas nuevas: `"sku"` y `"barcode"`. Nada más cambia de forma — no se toca `REQUIRED_FIELDS` (expense tampoco los declara requeridos) ni `SINGLE_VALUE_FIELDS` (expense tampoco los declara escalares: dos columnas de sku en un archivo real no es el mismo tipo de colisión que dos columnas de monto).

- [ ] **Step 1: Escribir el test de catálogo (falla)**

```python
# backend/app/tests/api/v1/test_field_catalog_sale_sku_fs0.py
"""F-S.0 mecanismo 1: sku/barcode tienen que existir como target de venta.

Sin esto el <select> del panel de mapeo nunca ofrece la opción — el usuario no
puede declarar "esta columna es el código del producto" en una hoja de ventas,
aunque el motor de resolución ya sepa leerlo (ver ingestion_import_service.py:
_venta_producto_id).
"""

from __future__ import annotations

from httpx import AsyncClient

from app.persistence.models.tenant import Tenant


async def test_field_catalog_expone_sku_y_barcode_para_venta(
    client: AsyncClient, sample_tenant: Tenant, auth_headers: dict[str, str]
) -> None:
    resp = await client.get("/ingestion/field-catalog", headers=auth_headers)
    assert resp.status_code == 200
    catalog = resp.json()

    sale_fields = {f["value"]: f for f in catalog["sale"]["fields"]}
    assert "sku" in sale_fields, "sku no está en CANONICAL_FIELDS['sale']"
    assert "barcode" in sale_fields, "barcode no está en CANONICAL_FIELDS['sale']"
    # No son obligatorios ni escalares — mismo criterio que en 'expense'.
    assert "sku" not in catalog["sale"]["required"]
    assert "barcode" not in catalog["sale"]["required"]
    assert sale_fields["sku"]["single_value"] is False
    assert sale_fields["barcode"]["single_value"] is False
```

Si el proyecto no tiene ya un fixture `auth_headers`/`client` con esa forma exacta, usar el mismo patrón que `backend/app/tests/api/v1/test_ingestion.py` (mismo directorio) — copiar sus fixtures de auth en vez de inventar uno nuevo.

- [ ] **Step 2: Correr y verificar que falla**

Run: `.venv/bin/python -m pytest app/tests/api/v1/test_field_catalog_sale_sku_fs0.py -v --no-cov`
Expected: FAIL en `assert "sku" in sale_fields`.

- [ ] **Step 3: Agregar los dos targets**

En `backend/app/application/services/column_mapping_service.py`, dentro de `CANONICAL_FIELDS["sale"]` (línea 35), agregar después de `"customer_name": "Cliente — Nombre",` (línea 54):

```python
        # F-S.0: identifican el producto de ESTA venta, igual que ya hacen en
        # 'expense' (líneas de abajo). El motor de resolución ya los lee
        # (`_venta_producto_id`, `_resolve_product`) — faltaba el target para
        # que el usuario pudiera mapear la columna.
        "sku": "Código (SKU)",
        "barcode": "Código de barras (EAN/UPC)",
```

- [ ] **Step 4: Correr y verificar que pasa**

Run: `.venv/bin/python -m pytest app/tests/api/v1/test_field_catalog_sale_sku_fs0.py -v --no-cov`
Expected: PASS

- [ ] **Step 5: Escribir el test end-to-end (probablemente YA pasa — es de regresión, no de comportamiento nuevo)**

```python
# backend/app/tests/services/test_ingestion_sale_link_by_code_fs0.py
"""F-S.0 mecanismo 1, end-to-end: una venta con SKU mapeado vincula por código
aunque el nombre de la fila no coincida con el del catálogo (variante de
nombre, error de tipeo, lo que sea) — el código gana sobre el nombre, igual
que ya hace `_resolve_product` para compras/gastos.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def test_venta_vincula_por_sku_mapeado_aunque_el_nombre_no_matchee(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola 500ml",
        sku="COCA-500",
        sale_price_ars=Decimal("1500"),
        stock_units=10,
    )
    db_session.add(product)
    await db_session.flush()

    summary = {
        "file_type": "spreadsheet",
        "mapping_contexts": [
            {"context_id": "c1", "entity_type": "sale", "label": "Ventas"},
        ],
        "ventas_detectadas": [
            {
                "fecha": "2026-08-01",
                "monto": "1500",
                # Nombre deliberadamente distinto al del catálogo — sólo el
                # código mapeado puede resolverlo.
                "articulo_vendido": "Gaseosa cola cualquiera",
                "codigo_interno": "COCA-500",
                "__context__": "c1",
            }
        ],
    }
    context_mappings = {
        "c1": {
            "fecha": "transaction_date",
            "monto": "amount",
            "articulo_vendido": "product_name",
            "codigo_interno": "sku",
        }
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"c1": True},
    )

    assert counts["ventas"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id == product.id, (
        "la venta tenía que vincular por SKU mapeado, no por nombre"
    )
```

- [ ] **Step 6: Correr y verificar que pasa (si falla, es una regresión real — investigar antes de seguir)**

Run: `.venv/bin/python -m pytest app/tests/services/test_ingestion_sale_link_by_code_fs0.py -v --no-cov`
Expected: PASS

- [ ] **Step 7: Lint + typecheck sobre el archivo tocado**

Run: `ruff check app/application/services/column_mapping_service.py && mypy app/application/services/column_mapping_service.py`
Expected: sin errores nuevos.

- [ ] **Step 8: Commit**

```bash
git add app/application/services/column_mapping_service.py \
        app/tests/api/v1/test_field_catalog_sale_sku_fs0.py \
        app/tests/services/test_ingestion_sale_link_by_code_fs0.py
git commit -m "$(cat <<'EOF'
feat(ingesta): una venta no podía declarar el código de su producto

CANONICAL_FIELDS['sale'] no tenía sku/barcode (expense sí, desde F-H6.a) — el
motor de resolución ya sabía leerlos, pero el <select> del panel nunca los
ofrecía para una hoja de ventas. F-S.0 mecanismo 1.

Co-Authored-By: claude-flow <ruv@ruv.net>
EOF
)"
```

---

### Task 2: registrar barcode en el índice transaccional (cierra el gap de F-H1)

**Contexto medido:** `_register_product_transaction_indexes` (`ingestion_import_service.py:1741-1771`) registra un producto recién creado/vinculado en `by_sku`/`by_name`/`by_token` para que ventas/gastos POSTERIORES del MISMO archivo lo encuentren (comentario `F-H1`, línea 1749). Se llama en 3 sitios: compra en el path plano (línea 4145), compra en el path multi-hoja (línea 5771) y catálogo en el path multi-hoja (línea 6218) — este último con el comentario explícito "antes un producto creado por catálogo era invisible para las ventas del mismo archivo". **Pero la función NO recibe `barcode`**, así que un producto creado por catálogo en el mismo archivo con barcode SÍ se resuelve por sku/nombre pero NO por barcode hasta la PRÓXIMA corrida (cuando `_load_product_identity_indexes` lo recarga de la base). F-S.0 mecanismo 1 agrega barcode como target de venta — sin este fix, esa columna no sirve para el caso más común que F-S.0 existe para resolver (catálogo + ventas en el MISMO archivo, el caso de ASTERIA).

**Files:**
- Modify: `backend/app/application/services/ingestion_import_service.py:1741-1771` (función `_register_product_transaction_indexes`)
- Modify: `backend/app/application/services/ingestion_import_service.py:4145-4147` (call site compra, path plano)
- Modify: `backend/app/application/services/ingestion_import_service.py:5771-5773` (call site compra, path multi-hoja)
- Modify: `backend/app/application/services/ingestion_import_service.py:6218-6220` (call site catálogo, path multi-hoja)
- Test: `backend/app/tests/services/test_ingestion_same_file_barcode_link_fs0.py` (create)

**Interfaces:**
- Consumes: `ProductIdentityIndexes.by_barcode: dict[str, list[uuid.UUID]]` (NamedTuple field ya existente, `ingestion_import_service.py:1322-1329`) — es un dict mutable dentro de una NamedTuple inmutable, así que se puede escribir en el mismo objeto sin reasignar la tupla. `normalize_barcode(s: str | None) -> str | None` (`app/domain/text_norm.py:70`, ya importado en el archivo).
- Produces: `_register_product_transaction_indexes(..., *, barcode: str | None = None, by_barcode: dict[str, list[uuid.UUID]] | None = None) -> None` — firma retrocompatible (los dos params nuevos son keyword-only con default `None`, los 3 call sites existentes que NO los pasen siguen compilando, pero los 3 los van a pasar en este task).

- [ ] **Step 1: Escribir el test (falla)**

```python
# backend/app/tests/services/test_ingestion_same_file_barcode_link_fs0.py
"""F-S.0 mecanismo 2 (gap fix): un producto creado por una hoja de catálogo
tiene que quedar vinculable por BARCODE para las ventas del MISMO archivo, no
solo por sku/nombre (F-H1 ya lo hacía para esos dos, ver
`_register_product_transaction_indexes`). Sin esto, una venta que declara el
barcode de un producto recién creado por el catálogo adjunto no resuelve hasta
la corrida SIGUIENTE — justo el caso que F-S.0 existe para arreglar.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as importer
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def test_venta_vincula_por_barcode_de_producto_creado_en_el_mismo_archivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "mapping_contexts": [
            {"context_id": "cat", "entity_type": "product", "label": "Catálogo"},
            {"context_id": "vta", "entity_type": "sale", "label": "Ventas"},
        ],
        "stock_detectado": [
            {
                "nombre": "Coca Cola 500ml",
                "cod_barras": "7791234567890",
                "precio": "1500",
                "stock": "10",
                "__context__": "cat",
            }
        ],
        "ventas_detectadas": [
            {
                "fecha": "2026-08-01",
                "monto": "1500",
                # Nombre deliberadamente distinto: solo el barcode puede
                # resolverlo, y el producto NO existía antes de esta corrida.
                "articulo_vendido": "Gaseosa sin marca",
                "cod_barras_venta": "7791234567890",
                "__context__": "vta",
            }
        ],
    }
    context_mappings = {
        "cat": {"nombre": "name", "cod_barras": "barcode", "precio": "sale_price_ars",
                "stock": "stock_units"},
        "vta": {"fecha": "transaction_date", "monto": "amount",
                "articulo_vendido": "product_name", "cod_barras_venta": "barcode"},
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"cat": True, "vta": True},
    )

    assert counts["productos"] == 1
    assert counts["ventas"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is not None, (
        "la venta tenía que vincular por barcode contra el producto que el "
        "catálogo del MISMO archivo acaba de crear"
    )
```

- [ ] **Step 2: Correr y verificar que falla**

Run: `.venv/bin/python -m pytest app/tests/services/test_ingestion_same_file_barcode_link_fs0.py -v --no-cov`
Expected: FAIL en `assert sale.product_id is not None` (o el conteo, si la venta cae a "Otros"/sin vincular en vez de insertarse sin producto — verificar cuál pasa realmente y ajustar el mensaje del assert si hace falta, pero NO relajar el assert de fondo: el punto es que `product_id` tiene que quedar seteado).

- [ ] **Step 3: Extender la función**

En `backend/app/application/services/ingestion_import_service.py:1741`, reemplazar:

```python
def _register_product_transaction_indexes(
    product_id: uuid.UUID,
    name: str | None,
    sku: str | None,
    by_sku: dict[str, uuid.UUID],
    by_name: dict[str, uuid.UUID | None],
    by_token: dict[str, set[uuid.UUID]],
) -> None:
```

por:

```python
def _register_product_transaction_indexes(
    product_id: uuid.UUID,
    name: str | None,
    sku: str | None,
    by_sku: dict[str, uuid.UUID],
    by_name: dict[str, uuid.UUID | None],
    by_token: dict[str, set[uuid.UUID]],
    *,
    barcode: str | None = None,
    by_barcode: dict[str, list[uuid.UUID]] | None = None,
) -> None:
```

y agregar al final del cuerpo de la función (después del bloque `for tok in _product_name_tokens(clean_name): ...`, antes del cierre de la función, línea 1771):

```python
    # F-S.0: mismo motivo que sku/nombre arriba (comentario F-H1) — sin esto,
    # un producto creado por catálogo en este archivo es invisible por
    # barcode para las ventas del MISMO archivo hasta la corrida siguiente.
    if barcode and by_barcode is not None:
        bc_key = normalize_barcode(barcode)
        if bc_key:
            by_barcode.setdefault(bc_key, [])
            if product_id not in by_barcode[bc_key]:
                by_barcode[bc_key].append(product_id)
```

Actualizar el docstring de la función para mencionar barcode (agregar una línea después de la explicación existente).

- [ ] **Step 4: Actualizar los 3 call sites**

En `ingestion_import_service.py:4145-4147` (compra, path plano — dentro de `_add_expense`/equivalente del path plano):

```python
                            if _pid is not None:
                                _register_product_transaction_indexes(
                                    _pid, _exp_name, _exp_sku, _by_sku, _by_name, _by_token,
                                    barcode=_exp_barcode, by_barcode=_identity_indexes.by_barcode,
                                )
```

En `ingestion_import_service.py:5771-5773` (compra, path multi-hoja):

```python
            if _pid is not None:
                _register_product_transaction_indexes(
                    _pid, _exp_name, _exp_sku, _by_sku, _by_name, _by_token,
                    barcode=_exp_barcode, by_barcode=_identity_indexes.by_barcode,
                )
```

En `ingestion_import_service.py:6218-6220` (catálogo, path multi-hoja):

```python
            _register_product_transaction_indexes(
                _new_id, name, sku, _by_sku, _by_name, _by_token,
                barcode=barcode, by_barcode=_identity_indexes.by_barcode,
            )
```

(`_exp_barcode` ya existe en scope en ambos call sites de compra — confirmado en `4032` y `5664`; `barcode` ya existe en scope del call site de catálogo — confirmado en `5915`. `_identity_indexes` ya está en scope en los tres — es el mismo objeto que ya reciben como parámetro/closure.)

- [ ] **Step 5: Correr y verificar que pasa**

Run: `.venv/bin/python -m pytest app/tests/services/test_ingestion_same_file_barcode_link_fs0.py -v --no-cov`
Expected: PASS

- [ ] **Step 6: Correr la suite de identidad de producto completa (zona de alto riesgo — no romper F2/F5)**

Run: `.venv/bin/python -m pytest app/tests/services/test_product_identity_import_e2e.py app/tests/services/test_ingestion_product_identity.py app/tests/services/test_product_identity_import_e2e.py -v --no-cov`
Expected: todos PASS, mismo conteo que antes del cambio.

- [ ] **Step 7: Lint + typecheck**

Run: `ruff check app/application/services/ingestion_import_service.py && mypy app/application/services/ingestion_import_service.py`

- [ ] **Step 8: Commit**

```bash
git add app/application/services/ingestion_import_service.py \
        app/tests/services/test_ingestion_same_file_barcode_link_fs0.py
git commit -m "$(cat <<'EOF'
fix(ingesta): un producto creado por catálogo no quedaba vinculable por código de barras en el mismo archivo

F-H1 ya registraba sku y nombre en el índice transaccional para que ventas
del MISMO archivo encontraran un producto recién creado por su hoja de
catálogo — pero no barcode. F-S.0 agrega barcode como target de venta
(commit anterior) y sin este fix esa columna no servía para el caso central
de F-S.0: catálogo y ventas en la misma carga.

Co-Authored-By: claude-flow <ruv@ruv.net>
EOF
)"
```

---

### Task 3: alias de producto persistido

**Contexto medido:** `_load_product_index` (`ingestion_import_service.py:1267-1300`) construye `by_name` únicamente desde `Product.name`. No existe hoy ningún lugar donde un nombre "vinculado a mano" quede guardado — cada import repite el mismo trabajo de matching manual. El patrón de flag en `custom_fields` que el resto del código usa (`_sentinel`, `_brand_collapsed`, `_vektor_costo_base`) siempre reasigna el dict entero: `product.custom_fields = {**(product.custom_fields or {}), "key": value}` (ver `ingestion_import_service.py:2252-2255`).

**Files:**
- Create: `backend/app/domain/product_alias.py`
- Modify: `backend/app/application/services/ingestion_import_service.py:1267-1300` (`_load_product_index`)
- Test: `backend/app/tests/domain/test_product_alias_fs0.py` (create)
- Test: `backend/app/tests/services/test_ingestion_product_index_alias_fs0.py` (create)

**Interfaces:**
- Produces:
  - `ALIASES_FIELD: str = "_aliases"` (constante, `app/domain/product_alias.py`)
  - `add_alias(custom_fields: dict[str, Any] | None, raw_name: str) -> dict[str, Any]` — devuelve un dict NUEVO (nunca muta el de entrada), idempotente, ignora nombres vacíos.
  - `product_aliases(custom_fields: dict[str, Any] | None) -> list[str]` — lee los alias guardados, `[]` si no hay.
- Consumes (Task 4 los va a usar): ambas funciones de `app.domain.product_alias`.

- [ ] **Step 1: Escribir el test del helper puro (falla — el módulo no existe)**

```python
# backend/app/tests/domain/test_product_alias_fs0.py
"""F-S.0 mecanismo 3: el alias es una decisión humana persistida, nunca
inferida. `add_alias` es puro (no muta el dict de entrada) e idempotente."""

from __future__ import annotations

from app.domain.product_alias import ALIASES_FIELD, add_alias, product_aliases


def test_add_alias_agrega_a_una_lista_nueva() -> None:
    result = add_alias(None, "Gaseosa cola cualquiera")
    assert result == {ALIASES_FIELD: ["Gaseosa cola cualquiera"]}


def test_add_alias_no_muta_el_dict_de_entrada() -> None:
    original = {ALIASES_FIELD: ["ya existía"]}
    result = add_alias(original, "nuevo alias")
    assert original == {ALIASES_FIELD: ["ya existía"]}, "no se puede mutar in-place"
    assert result == {ALIASES_FIELD: ["ya existía", "nuevo alias"]}


def test_add_alias_es_idempotente() -> None:
    once = add_alias(None, "Coca")
    twice = add_alias(once, "Coca")
    assert twice == once


def test_add_alias_ignora_nombre_vacio() -> None:
    result = add_alias({"marca": "Coca-Cola"}, "   ")
    assert result == {"marca": "Coca-Cola"}


def test_add_alias_preserva_otras_claves() -> None:
    result = add_alias({"marca": "Coca-Cola"}, "Gaseosa")
    assert result == {"marca": "Coca-Cola", ALIASES_FIELD: ["Gaseosa"]}


def test_product_aliases_vacio_sin_datos() -> None:
    assert product_aliases(None) == []
    assert product_aliases({}) == []
```

- [ ] **Step 2: Correr y verificar que falla**

Run: `.venv/bin/python -m pytest app/tests/domain/test_product_alias_fs0.py -v --no-cov`
Expected: FAIL con `ModuleNotFoundError: No module named 'app.domain.product_alias'`

- [ ] **Step 3: Crear el módulo**

```python
# backend/app/domain/product_alias.py
"""Alias de nombre persistido para un producto (F-S.0, mecanismo 3).

Un alias es un nombre con el que un ARCHIVO llamó al producto y que el
usuario vinculó a mano a uno ya existente — nunca se infiere solo, es
exactamente lo opuesto a inventar: es la decisión humana que ya se tomó una
vez, guardada para no repetirla. Vive en ``custom_fields["_aliases"]`` porque
no es un dato de negocio del producto (no aparece en la ficha), es una pista
de matching para la PRÓXIMA importación. Mismo patrón de flag en
``custom_fields`` que ``_sentinel``/``_brand_collapsed``/``_vektor_costo_base``.
"""

from __future__ import annotations

from typing import Any

ALIASES_FIELD = "_aliases"


def add_alias(custom_fields: dict[str, Any] | None, raw_name: str) -> dict[str, Any]:
    """``custom_fields`` NUEVO con ``raw_name`` agregado a los alias.

    No muta el dict de entrada (reasignación completa, igual que el resto del
    código sobre esta columna: no hay ``MutableDict`` en el modelo, así que
    una mutación in-place no se detectaría como cambio). Idempotente: un
    alias ya presente no se duplica. Se guarda tal cual lo escribió el
    archivo (para mostrarlo en pantalla) — el matching normaliza recién al
    indexar, no acá.
    """
    cleaned = raw_name.strip()
    base = dict(custom_fields or {})
    if not cleaned:
        return base
    existing = list(base.get(ALIASES_FIELD) or [])
    if cleaned in existing:
        return base
    return {**base, ALIASES_FIELD: [*existing, cleaned]}


def product_aliases(custom_fields: dict[str, Any] | None) -> list[str]:
    """Alias guardados de un producto, o lista vacía."""
    return list((custom_fields or {}).get(ALIASES_FIELD) or [])
```

- [ ] **Step 4: Correr y verificar que pasa**

Run: `.venv/bin/python -m pytest app/tests/domain/test_product_alias_fs0.py -v --no-cov`
Expected: PASS (6 tests)

- [ ] **Step 5: Escribir el test de indexación (falla)**

```python
# backend/app/tests/services/test_ingestion_product_index_alias_fs0.py
"""F-S.0 mecanismo 3: `_load_product_index` tiene que resolver también por
alias, no solo por Product.name — si no, guardar el alias no sirve para nada
en la SIGUIENTE importación."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as importer
from app.domain.product_alias import add_alias
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


async def test_load_product_index_resuelve_por_alias(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola 500ml",
        sale_price_ars=Decimal("1500"),
        stock_units=10,
        custom_fields=add_alias(None, "Gaseosa cola cualquiera"),
    )
    db_session.add(product)
    await db_session.flush()

    by_sku, by_name, by_token = await importer._load_product_index(
        db_session, sample_tenant.tenant_id
    )

    from app.domain.text_norm import normalize_product_name  # noqa: PLC0415

    alias_key = normalize_product_name("Gaseosa cola cualquiera")
    assert by_name.get(alias_key) == product.id, (
        "el alias guardado tiene que resolver igual que el nombre real"
    )
```

- [ ] **Step 6: Correr y verificar que falla**

Run: `.venv/bin/python -m pytest app/tests/services/test_ingestion_product_index_alias_fs0.py -v --no-cov`
Expected: FAIL en el `assert by_name.get(alias_key)`.

- [ ] **Step 7: Extender `_load_product_index`**

En `backend/app/application/services/ingestion_import_service.py`, agregar el import al tope del archivo (junto a los otros imports de `app.domain`):

```python
from app.domain.product_alias import product_aliases
```

Reemplazar el cuerpo de `_load_product_index` (línea 1267-1300):

```python
async def _load_product_index(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID | None], dict[str, set[uuid.UUID]]]:
    """Carga el catálogo del tenant UNA vez para vincular transacciones en memoria.

    Evita N queries (una por fila). Devuelve `(by_sku, by_name, by_token)`:
    - `by_sku[sku_lower] = product_id`
    - `by_name[norm_name] = product_id` o `None` si el nombre normalizado es
      ambiguo (varios productos lo comparten → no se vincula). Incluye tanto
      `Product.name` como los alias guardados en `custom_fields["_aliases"]`
      (F-S.0): un alias que otro producto también reclama es igual de
      ambiguo que un nombre real repetido.
    - `by_token[token] = {product_id, ...}` para match conservador por tokens
      (Mejora B): token ≥3 chars del nombre, sin stopwords genéricos de unidad.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.product import Product  # noqa: PLC0415

    result = await session.execute(
        select(Product.id, Product.name, Product.sku, Product.custom_fields).where(
            Product.tenant_id == tenant_id
        )
    )
    by_sku: dict[str, uuid.UUID] = {}
    by_name: dict[str, uuid.UUID | None] = {}
    by_token: dict[str, set[uuid.UUID]] = {}
    for pid, pname, psku, pcustom in result.all():
        sku_key = normalize_sku(psku)
        if sku_key:
            by_sku[sku_key] = pid
        for candidate_name in (pname, *product_aliases(pcustom)):
            norm = _normalize_name(candidate_name or "")
            if norm:
                by_name[norm] = pid if norm not in by_name else None  # None = ambiguo
        for tok in _product_name_tokens(pname or ""):
            by_token.setdefault(tok, set()).add(pid)
    return by_sku, by_name, by_token
```

Nota: los tokens (`by_token`) siguen saliendo SOLO del nombre real, no de los alias — el tier de tokens ya es el más débil (match conservador por intersección) y un alias corto sumaría ruido a un tier pensado para desambiguar, no para ampliar candidatos.

- [ ] **Step 8: Correr y verificar que pasa**

Run: `.venv/bin/python -m pytest app/tests/services/test_ingestion_product_index_alias_fs0.py -v --no-cov`
Expected: PASS

- [ ] **Step 9: Correr toda la suite de indexación de producto (no romper nada existente)**

Run: `.venv/bin/python -m pytest app/tests/services/test_product_identity_import_e2e.py app/tests/services/test_ingestion_product_identity.py -v --no-cov`
Expected: mismo resultado que antes de este cambio.

- [ ] **Step 10: Lint + typecheck**

Run: `ruff check app/domain/product_alias.py app/application/services/ingestion_import_service.py && mypy app/domain/product_alias.py app/application/services/ingestion_import_service.py`

- [ ] **Step 11: Commit**

```bash
git add app/domain/product_alias.py \
        app/application/services/ingestion_import_service.py \
        app/tests/domain/test_product_alias_fs0.py \
        app/tests/services/test_ingestion_product_index_alias_fs0.py
git commit -m "$(cat <<'EOF'
feat(productos): vincular un nombre a mano no dejaba rastro para la próxima importación

Cada archivo repetía el mismo trabajo de matching manual porque no había
dónde guardar la decisión. F-S.0 mecanismo 3: custom_fields['_aliases'],
`_load_product_index` ahora resuelve también por alias.

Co-Authored-By: claude-flow <ruv@ruv.net>
EOF
)"
```

---

### Task 4: cola de ventas sin producto, agrupada por nombre

**Contexto medido:** hoy una venta que no resuelve producto queda con `product_id = NULL` en silencio — no hay contador (`counts` no tiene `ventas_sin_producto`, verificado por grep), no hay warning en el confirm (comparar con `counts.get("sin_producto")` que SÍ genera warning para compras, `app/api/v1/ingestion.py:3151-3155`) y no hay forma de vincularlas en bloque. El patrón de warning a seguir es exactamente ese bloque de `ingestion.py`. El patrón de re-validación de un `target_product_id` contra tenant+activo es el de `others.py:474-484` (`reclassify_record`).

**Files:**
- Modify: `backend/app/application/services/ingestion_import_service.py:3876-3884` (path plano, resolución de venta)
- Modify: `backend/app/application/services/ingestion_import_service.py:5276-5278` (path multi-hoja, `_add_sale`)
- Modify: `backend/app/api/v1/ingestion.py:3145-3155` (warnings del confirm)
- Modify: `backend/app/api/v1/sales.py` (agregar 2 endpoints nuevos)
- Test: `backend/app/tests/services/test_ingestion_ventas_sin_producto_fs0.py` (create)
- Test: `backend/app/tests/api/v1/test_sales_product_link_queue_fs0.py` (create)

**Interfaces:**
- Consumes: `add_alias`/`product_aliases` de `app.domain.product_alias` (Task 3); `require_modify_access` (`app/api/v1/deps.py`, ya importado en `sales.py:15`); `get_current_tenant` (mismo módulo).
- Produces:
  - `UNLINKED_PRODUCT_NAME_FIELD: str = "_unlinked_product_name_raw"` (constante nueva, junto a `IMPORT_CONTEXT_FIELD` en `ingestion_import_service.py` — mismo patrón de constante de clave de `custom_fields`).
  - `counts["ventas_sin_producto"]` (int) en ambos paths de import.
  - `GET /sales/product-link-queue` → `{"groups": [{"raw_name": str, "count": int, "sample_sale_ids": list[str]}], "truncated": bool}`
  - `POST /sales/product-link-queue/link` body `{"raw_name": str, "target_product_id": UUID}` → `{"linked": int}`

- [ ] **Step 1: Escribir el test de conteo (falla)**

```python
# backend/app/tests/services/test_ingestion_ventas_sin_producto_fs0.py
"""F-S.0 mecanismo 4: una venta con nombre de producto que no resuelve contra
el catálogo se cuenta (`ventas_sin_producto`) y guarda el nombre crudo en
custom_fields — nunca se pierde en silencio, nunca se inventa un producto."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as importer
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def test_venta_sin_producto_se_cuenta_y_guarda_el_nombre_crudo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "mapping_contexts": [
            {"context_id": "vta", "entity_type": "sale", "label": "Ventas"},
        ],
        "ventas_detectadas": [
            {
                "fecha": "2026-08-01",
                "monto": "1500",
                "articulo_vendido": "Producto que no está en ningún catálogo",
                "__context__": "vta",
            }
        ],
    }
    context_mappings = {
        "vta": {"fecha": "transaction_date", "monto": "amount",
                "articulo_vendido": "product_name"},
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"vta": True},
    )

    assert counts["ventas"] == 1
    assert counts["ventas_sin_producto"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is None
    assert sale.custom_fields.get("_unlinked_product_name_raw") == (
        "Producto que no está en ningún catálogo"
    )


async def test_venta_sin_nombre_de_producto_no_cuenta_como_sin_producto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una venta que NUNCA declaró producto (venta de mostrador, sin columna
    de nombre) no es lo mismo que una que declaró un nombre y no resolvió —
    no hay nada que ofrecer en la cola de vinculación."""
    summary = {
        "file_type": "spreadsheet",
        "mapping_contexts": [
            {"context_id": "vta", "entity_type": "sale", "label": "Ventas"},
        ],
        "ventas_detectadas": [
            {"fecha": "2026-08-01", "monto": "1500", "__context__": "vta"}
        ],
    }
    context_mappings = {
        "vta": {"fecha": "transaction_date", "monto": "amount"},
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"vta": True},
    )

    assert counts["ventas"] == 1
    assert counts.get("ventas_sin_producto", 0) == 0
```

- [ ] **Step 2: Correr y verificar que falla**

Run: `.venv/bin/python -m pytest app/tests/services/test_ingestion_ventas_sin_producto_fs0.py -v --no-cov`
Expected: FAIL — `counts["ventas_sin_producto"]` no existe (KeyError) y `custom_fields.get("_unlinked_product_name_raw")` es `None`.

- [ ] **Step 3: Agregar la constante**

En `ingestion_import_service.py`, junto a la definición de `IMPORT_CONTEXT_FIELD` (buscar con `grep -n "IMPORT_CONTEXT_FIELD ="`), agregar al lado:

```python
# F-S.0: nombre CRUDO que el archivo usó para una venta que no resolvió
# producto — nunca se inventa un match, se conserva para que la cola de
# vinculación (POST /sales/product-link-queue/link) lo pueda ofrecer agrupado.
UNLINKED_PRODUCT_NAME_FIELD = "_unlinked_product_name_raw"
```

- [ ] **Step 4: Instrumentar el path multi-hoja (`_add_sale`, línea ~5276-5278)**

Reemplazar:

```python
        cf = _custom_fields(row, cf_cols)
        _registrar_monto_derivado(cf, _linea, counts)
        # FASE 3 + F2-T5: link al catálogo (barcode → sku → nombre → tokens).
        _venta_producto = _venta_nombre_producto(row, cols)
        entry.product_id = _venta_producto_id(row, cols)
```

por:

```python
        cf = _custom_fields(row, cf_cols)
        _registrar_monto_derivado(cf, _linea, counts)
        # FASE 3 + F2-T5: link al catálogo (barcode → sku → nombre → tokens).
        _venta_producto = _venta_nombre_producto(row, cols)
        entry.product_id = _venta_producto_id(row, cols)
        # F-S.0: la fila DECLARÓ un producto y no resolvió — se cuenta y se
        # conserva el nombre crudo para la cola de vinculación. Distinto de
        # una venta que nunca declaró nombre (venta de mostrador): esa no
        # entra acá, no hay nada que ofrecer para vincular.
        _venta_producto_raw = _clean_str(_venta_producto, 299)
        if entry.product_id is None and _venta_producto_raw:
            counts["ventas_sin_producto"] = counts.get("ventas_sin_producto", 0) + 1
            cf[UNLINKED_PRODUCT_NAME_FIELD] = _venta_producto_raw
```

- [ ] **Step 5: Instrumentar el path plano (línea ~3876-3884)**

Reemplazar:

```python
                    # FASE 3 + F2-T5: link al catálogo (barcode → sku → nombre → tokens).
                    entry.product_id = _resolve_product(
                        _by_sku,
                        _by_name,
                        row.get(nombre_col) if nombre_col else None,
                        row.get(sku_col) if sku_col else None,
                        _by_token,
                        by_barcode=_identity_indexes.by_barcode,
                        barcode=row.get(barcode_col) if barcode_col else None,
                    )
```

por:

```python
                    # FASE 3 + F2-T5: link al catálogo (barcode → sku → nombre → tokens).
                    _venta_nombre_raw = row.get(nombre_col) if nombre_col else None
                    entry.product_id = _resolve_product(
                        _by_sku,
                        _by_name,
                        _venta_nombre_raw,
                        row.get(sku_col) if sku_col else None,
                        _by_token,
                        by_barcode=_identity_indexes.by_barcode,
                        barcode=row.get(barcode_col) if barcode_col else None,
                    )
                    # F-S.0: mismo criterio que el path multi-hoja — ver el
                    # comentario en `_add_sale`.
                    _venta_nombre_raw_clean = _clean_str(_venta_nombre_raw, 299)
                    if entry.product_id is None and _venta_nombre_raw_clean:
                        counts["ventas_sin_producto"] = (
                            counts.get("ventas_sin_producto", 0) + 1
                        )
                        cf[UNLINKED_PRODUCT_NAME_FIELD] = _venta_nombre_raw_clean
```

(`cf` ya existe en scope en ese punto del path plano — se define un poco más arriba, línea 3857, y se asigna a `entry.custom_fields` más abajo, línea 3918 — verificar que la asignación a `entry.custom_fields` sigue siendo POSTERIOR a este bloque; si algún refactor futuro reordena esto, el custom_field se perdería en silencio.)

- [ ] **Step 6: Correr y verificar que pasa**

Run: `.venv/bin/python -m pytest app/tests/services/test_ingestion_ventas_sin_producto_fs0.py -v --no-cov`
Expected: PASS (2 tests)

- [ ] **Step 7: Agregar el warning al confirm**

En `backend/app/api/v1/ingestion.py`, dentro del bloque de warnings (después de las líneas 3151-3155, el bloque de `counts.get("sin_producto")` de compras), agregar:

```python
    if counts.get("ventas_sin_producto"):
        warnings.append(
            f"{counts['ventas_sin_producto']} venta(s) con un nombre de producto que no "
            "coincidió con el catálogo quedaron sin vincular. Podés resolverlas agrupadas "
            "por nombre desde la cola de vinculación."
        )
```

- [ ] **Step 8: Escribir el test de los endpoints (falla — no existen)**

```python
# backend/app/tests/api/v1/test_sales_product_link_queue_fs0.py
"""F-S.0 mecanismo 4: la cola agrupa ventas sin producto por nombre crudo y
permite vincularlas en bloque, dejando alias para la próxima importación."""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.product_alias import product_aliases
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def _unlinked_sale(
    db_session: AsyncSession, tenant_id, raw_name: str, amount: str = "1500"
) -> SaleEntry:
    from datetime import UTC, datetime

    entry = SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal(amount),
        quantity=1,
        transaction_date=datetime.now(UTC),
        payment_method="cash",
        notes="test",
        provenance="REAL",
        custom_fields={"_unlinked_product_name_raw": raw_name},
    )
    db_session.add(entry)
    await db_session.flush()
    return entry


async def test_queue_agrupa_por_nombre_crudo(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    await _unlinked_sale(db_session, sample_tenant.tenant_id, "Gaseosa cola grande")
    await _unlinked_sale(db_session, sample_tenant.tenant_id, "Gaseosa cola grande")
    await _unlinked_sale(db_session, sample_tenant.tenant_id, "Agua sin gas")
    await db_session.commit()

    resp = await client.get("/sales/product-link-queue", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    by_name = {g["raw_name"]: g["count"] for g in body["groups"]}
    assert by_name == {"Gaseosa cola grande": 2, "Agua sin gas": 1}


async def test_vincular_en_bloque_setea_product_id_y_deja_alias(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola 2L",
        sale_price_ars=Decimal("2000"),
        stock_units=5,
    )
    db_session.add(product)
    await db_session.flush()
    s1 = await _unlinked_sale(db_session, sample_tenant.tenant_id, "Gaseosa cola grande")
    s2 = await _unlinked_sale(db_session, sample_tenant.tenant_id, "Gaseosa cola grande")
    await db_session.commit()

    resp = await client.post(
        "/sales/product-link-queue/link",
        json={"raw_name": "Gaseosa cola grande", "target_product_id": str(product.id)},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["linked"] == 2

    await db_session.refresh(s1)
    await db_session.refresh(s2)
    assert s1.product_id == product.id
    assert s2.product_id == product.id
    assert s1.custom_fields.get("_unlinked_product_name_raw") is None, (
        "el flag queda obsoleto una vez vinculado, no tiene que seguir en la cola"
    )

    await db_session.refresh(product)
    assert "Gaseosa cola grande" in product_aliases(product.custom_fields), (
        "vincular a mano tiene que dejar alias para la próxima importación (mecanismo 3)"
    )


async def test_vincular_con_producto_de_otro_tenant_rechaza(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    import uuid

    resp = await client.post(
        "/sales/product-link-queue/link",
        json={"raw_name": "Lo que sea", "target_product_id": str(uuid.uuid4())},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "INVALID_TARGET_PRODUCT"
```

Si el proyecto usa otros nombres de fixtures (`client`/`db_session`/`sample_tenant`/`auth_headers`), copiar los que ya usa `app/tests/api/v1/test_sales.py` en vez de inventar — es el archivo hermano del que se está extendiendo.

- [ ] **Step 9: Correr y verificar que falla**

Run: `.venv/bin/python -m pytest app/tests/api/v1/test_sales_product_link_queue_fs0.py -v --no-cov`
Expected: FAIL — 404 en ambos endpoints (no existen todavía).

- [ ] **Step 10: Implementar los endpoints**

En `backend/app/api/v1/sales.py`, agregar los imports que falten al tope del archivo:

```python
from pydantic import BaseModel, Field

from app.domain.product_alias import add_alias
```

(`Product`, `select`, `AsyncSession`, `get_db_session`, `Tenant`, `require_modify_access`, `get_current_tenant` ya están importados — verificado arriba.)

Agregar los schemas y los dos endpoints al final del archivo:

```python
class ProductLinkQueueGroup(BaseModel):
    raw_name: str
    count: int
    sample_sale_ids: list[str]


class ProductLinkQueueResponse(BaseModel):
    groups: list[ProductLinkQueueGroup]
    truncated: bool


class LinkProductQueueRequest(BaseModel):
    raw_name: str = Field(min_length=1, max_length=299)
    target_product_id: UUID


class LinkProductQueueResponse(BaseModel):
    linked: int


# F-S.0 mecanismo 4: cuánto de la cola miramos por corrida. No es un límite de
# negocio (no hay "demasiadas ventas sin producto para importar") — es una
# guarda de memoria: se agrupa en Python, no en SQL, porque no hay precedente
# en el código de filtrar por clave de custom_fields en SQL y SQLite/Postgres
# difieren ahí (ver Global Constraints del plan F-S.0). `truncated` en la
# respuesta avisa si se cortó, nunca en silencio.
_PRODUCT_LINK_QUEUE_SCAN_LIMIT = 5000


@router.get(
    "/product-link-queue",
    response_model=ProductLinkQueueResponse,
    summary="Ventas sin producto vinculado, agrupadas por nombre",
)
async def get_product_link_queue(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ProductLinkQueueResponse:
    result = await session.execute(
        select(SaleEntry.id, SaleEntry.custom_fields)
        .where(
            SaleEntry.tenant_id == tenant.tenant_id,
            SaleEntry.product_id.is_(None),
            SaleEntry.voided_at.is_(None),
        )
        .limit(_PRODUCT_LINK_QUEUE_SCAN_LIMIT + 1)
    )
    rows = result.all()
    truncated = len(rows) > _PRODUCT_LINK_QUEUE_SCAN_LIMIT
    rows = rows[:_PRODUCT_LINK_QUEUE_SCAN_LIMIT]

    grouped: dict[str, list[str]] = {}
    for sale_id, cf in rows:
        raw_name = (cf or {}).get("_unlinked_product_name_raw")
        if not raw_name:
            continue
        grouped.setdefault(raw_name, []).append(str(sale_id))

    groups = [
        ProductLinkQueueGroup(
            raw_name=name, count=len(ids), sample_sale_ids=ids[:5]
        )
        for name, ids in sorted(grouped.items(), key=lambda kv: len(kv[1]), reverse=True)
    ]
    return ProductLinkQueueResponse(groups=groups, truncated=truncated)


@router.post(
    "/product-link-queue/link",
    response_model=LinkProductQueueResponse,
    summary="Vincula en bloque todas las ventas con ese nombre crudo a un producto",
)
async def link_product_queue_group(
    body: LinkProductQueueRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
    user: User = Depends(require_modify_access),
) -> LinkProductQueueResponse:
    # Re-validación: NUNCA confiar en el id que manda el cliente (mismo
    # criterio que others.py:479-484).
    target = await session.get(Product, body.target_product_id)
    if (
        target is None
        or target.tenant_id != tenant.tenant_id
        or not target.is_active
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_TARGET_PRODUCT"},
        )

    result = await session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant.tenant_id,
            SaleEntry.product_id.is_(None),
            SaleEntry.voided_at.is_(None),
        )
    )
    linked = 0
    for sale in result.scalars().all():
        cf = sale.custom_fields or {}
        if cf.get("_unlinked_product_name_raw") != body.raw_name:
            continue
        sale.product_id = target.id
        # El flag queda obsoleto una vez vinculada: no tiene sentido que la
        # venta siga apareciendo en la cola.
        sale.custom_fields = {
            k: v for k, v in cf.items() if k != "_unlinked_product_name_raw"
        }
        linked += 1

    if linked:
        target.custom_fields = add_alias(target.custom_fields, body.raw_name)

    await session.flush()
    trigger_score_recalculation(session, str(tenant.tenant_id))
    return LinkProductQueueResponse(linked=linked)
```

Verificar que `status` (de `fastapi`) y `HTTPException` ya están importados en el archivo (ambos aparecen en la lista de imports leída al principio de esta tarea — si no, agregarlos). `trigger_score_recalculation` ya está importado (línea 23 del archivo, confirmado). `User` ya está importado (línea 29).

- [ ] **Step 11: Correr y verificar que pasa**

Run: `.venv/bin/python -m pytest app/tests/api/v1/test_sales_product_link_queue_fs0.py -v --no-cov`
Expected: PASS (3 tests)

- [ ] **Step 12: Correr la suite completa de ventas e ingestión (zona compartida, no romper nada)**

Run: `.venv/bin/python -m pytest app/tests/api/v1/test_sales.py app/tests/services/test_ingestion_ventas_sin_producto_fs0.py app/tests/api/v1/test_sales_product_link_queue_fs0.py app/tests/api/v1/test_ingestion.py --no-cov -q`
Expected: todos PASS.

- [ ] **Step 13: Lint + typecheck**

Run: `ruff check app/api/v1/sales.py app/api/v1/ingestion.py app/application/services/ingestion_import_service.py && mypy app/api/v1/sales.py app/api/v1/ingestion.py app/application/services/ingestion_import_service.py`

- [ ] **Step 14: Commit**

```bash
git add app/application/services/ingestion_import_service.py \
        app/api/v1/ingestion.py \
        app/api/v1/sales.py \
        app/tests/services/test_ingestion_ventas_sin_producto_fs0.py \
        app/tests/api/v1/test_sales_product_link_queue_fs0.py
git commit -m "$(cat <<'EOF'
feat(ventas): una venta sin producto que resolviera se perdía en silencio, sin cola para completarla

F-S.0 mecanismo 4: se cuenta (ventas_sin_producto), avisa en el confirm, y
GET/POST /sales/product-link-queue agrupa por nombre crudo y permite vincular
en bloque — dejando alias (mecanismo 3) para que la próxima importación
resuelva sola.

Co-Authored-By: claude-flow <ruv@ruv.net>
EOF
)"
```

---

## Cierre del plan — qué queda fuera a propósito

- **Frontend de la cola** (`/sales` o una pantalla dedicada que consuma `GET/POST /sales/product-link-queue`): no entra en este plan. Mismo patrón de secuenciación que F-O.3 en este mismo programa ("el backend ya devuelve... la pantalla no renderiza ninguno de los tres") — el backend queda completo y testeado, la UI es un fast-follow una vez que el usuario confirme que el diseño de la cola (agrupar por nombre, mostrar conteo, un botón "vincular a…") es el que quiere en pantalla.
- **F-S** (el SKU como identificador persistente — asignación, backfill, exponerlo en `/products`) es la fase siguiente en el orden declarado y depende de que F-CAT haya corrido antes del backfill (ya cerrado). No arranca en este plan.
- **Suggest-match automático en la cola** (ofrecer candidatos sugeridos por nombre/token al lado de cada grupo, como hace `/otros` con `match_candidates`): quedó fuera de Task 4 para no acoplar el endpoint nuevo al motor de identidad privado de `ingestion_import_service.py` (que no está pensado para invocarse fuera de una corrida de import). Es una mejora de UX razonable para un fast-follow, no bloquea la vinculación manual (el usuario ya puede buscar el producto por nombre desde el selector existente de `/products`).
