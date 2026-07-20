"""Cableado de los caminos de creación al motor de identidad (F5-A, A3).

Qué cubre y qué NO
------------------
Los tests de ``test_product_identity.py`` prueban el helper aislado. Acá se prueba
que **cada camino de escritura real** reacciona bien cuando el índice único rechaza
su INSERT: el import reusa y lo reporta como ``linked``, la API devuelve 409 en vez
de 500, y el balance suma en vez de pisar.

Los índices se crean dentro de la transacción del test (mismo patrón que
``f5_indexes``) porque sin ellos NO HAY COLISIÓN que disparar: estos tests pasarían
vacíos contra el schema actual. Cuando F5-B los declare en el ORM, el fixture se
borra y los tests siguen valiendo igual.

Para llegar al guard en la API hay que **sortear el pre-check** —si no, corta antes
y nunca se ejercita el camino nuevo—. Se lo parchea para que devuelva "no hay
colisión": eso es exactamente lo que ve una request que perdió la carrera TOCTOU,
no un atajo del test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.products import DEACTIVATION_REASON_MANUAL
from app.application.services import stock_service
from app.application.services.ingestion_import_service import (
    _resolve_purchase_identity,
    build_incomplete_product,
)
from app.application.services.product_identity import ProductIdentityConflictError
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_DDL = (
    "CREATE UNIQUE INDEX uq_products_tenant_barcode_norm ON products "
    "(tenant_id, barcode_normalized) WHERE is_active "
    "AND barcode_normalized IS NOT NULL AND barcode_normalized <> ''",
    "CREATE UNIQUE INDEX uq_products_tenant_sku_norm ON products "
    "(tenant_id, sku_normalized) WHERE is_active "
    "AND sku_normalized IS NOT NULL AND sku_normalized <> ''",
    "CREATE UNIQUE INDEX uq_inventory_balances_tenant_product ON inventory_balances "
    "(tenant_id, product_id)",
)
_INDEX_NAMES = (
    "uq_products_tenant_barcode_norm",
    "uq_products_tenant_sku_norm",
    "uq_inventory_balances_tenant_product",
)


@pytest_asyncio.fixture
async def f5_indexes(db_session: AsyncSession) -> AsyncGenerator[None, None]:
    """Los tres uniques de F5-B, dentro de la transacción del test."""
    for stmt in _DDL:
        await db_session.execute(text(stmt))
    await db_session.flush()
    yield
    for name in _INDEX_NAMES:
        await db_session.execute(text(f"DROP INDEX IF EXISTS {name}"))


def _product(tenant_id: uuid.UUID, **kw: Any) -> Product:
    base: dict[str, Any] = {
        "tenant_id": tenant_id,
        "name": "Producto",
        "sale_price_ars": Decimal("100"),
    }
    base.update(kw)
    return Product(**base)


# ── build_incomplete_product ─────────────────────────────────────────────────


@pytest.mark.usefixtures("f5_indexes")
async def test_build_incomplete_product_reusa_el_sku_ocupado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Violar el unique de SKU es un match exacto por clave FUERTE, no ambigüedad."""
    tid = sample_tenant.tenant_id
    ocupante = _product(tid, name="Coca 500", sku="COCA500")
    db_session.add(ocupante)
    await db_session.flush()

    pid, created = await build_incomplete_product(
        db_session, tid, "Coca Cola 500ml", "coca500", Decimal("50")
    )

    assert pid == ocupante.id, "debió reusar al ocupante del SKU"
    assert created is False, "reusar NO es crear: el import lo reportaría como creado"


@pytest.mark.usefixtures("f5_indexes")
async def test_build_incomplete_product_crea_cuando_la_clave_esta_libre(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El camino feliz sigue creando (el guard no debe volverse un no-op)."""
    pid, created = await build_incomplete_product(
        db_session, sample_tenant.tenant_id, "Producto nuevo", "NUEVO-1", Decimal("10")
    )
    assert pid is not None
    assert created is True


@pytest.mark.usefixtures("f5_indexes")
async def test_build_incomplete_product_cachea_el_producto_reusado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El cache debe apuntar al producto que realmente se va a usar.

    Con ``autoflush=False`` (prod) ``_apply_purchase_to_stock`` resuelve el producto
    por este cache. Si cacheara el candidato descartado en vez del ocupante, el
    stock se le sumaría a un producto que nunca se persistió.
    """
    tid = sample_tenant.tenant_id
    ocupante = _product(tid, name="Yerba", sku="YERBA1")
    db_session.add(ocupante)
    await db_session.flush()

    cache: dict[uuid.UUID, Any] = {}
    pid, _ = await build_incomplete_product(
        db_session, tid, "Yerba Mate", "yerba1", Decimal("10"), cache
    )

    assert pid is not None
    assert cache[pid].id == ocupante.id


@pytest.mark.usefixtures("f5_indexes")
async def test_build_incomplete_product_ambiguo_no_elige_ninguno(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Barcode de A y SKU de B: no hay "el existente" que reusar.

    Es la regresión del review #2 de la ronda 4: reusar acá vincularía la compra a
    un producto elegido por prioridad de índice, no por evidencia.
    """
    tid = sample_tenant.tenant_id
    a = _product(tid, name="A", barcode="7790011110001")
    b = _product(tid, name="B", sku="SKU-B")
    db_session.add_all([a, b])
    await db_session.flush()

    with pytest.raises(ProductIdentityConflictError) as exc:
        await build_incomplete_product(
            db_session, tid, "Mezcla", "SKU-B", Decimal("10"), barcode="7790011110001"
        )

    assert exc.value.ambiguous is True
    assert {p.id for p in exc.value.candidates} == {a.id, b.id}, (
        "la fila en /otros necesita AMBOS candidatos para ser revisable"
    )


# ── _resolve_purchase_identity ───────────────────────────────────────────────


@pytest.mark.usefixtures("f5_indexes")
async def test_compra_reusada_se_reporta_como_linked_no_created(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """`counts["productos_creados"]` no puede contar un producto que ya existía.

    El motor decide ``create`` porque su índice se pre-cargó al abrir la corrida y no
    ve al ocupante; el índice único lo corrige. Si esto devolviera ``"created"``, el
    resumen del import le mentiría al usuario.
    """
    from app.application.services.ingestion_import_service import (
        _load_product_identity_indexes,
    )

    tid = sample_tenant.tenant_id
    # Pre-carga ANTES de que exista el ocupante: es la foto que tiene una corrida de
    # import mientras otra transacción crea el producto.
    indexes = await _load_product_identity_indexes(db_session, tid)

    ocupante = _product(tid, name="Fideos", sku="FID-1")
    db_session.add(ocupante)
    await db_session.flush()

    action, pid, cands = await _resolve_purchase_identity(
        db_session,
        tid,
        name="Fideos secos",
        sku="fid-1",
        brand=None,
        barcode=None,
        unit_cost=Decimal("100"),
        indexes=indexes,
        cache={},
    )

    assert action == "linked"
    assert pid == ocupante.id
    assert cands == []


# ── stock_service._get_or_create_balance ─────────────────────────────────────


@pytest.mark.usefixtures("f5_indexes")
async def test_balance_duplicado_se_resuelve_al_existente(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El lock SHARED no serializa writers: dos requests crean dos balances.

    La carrera se simula haciendo que el SELECT de balances devuelva ``None`` la
    primera vez aunque la fila ya exista — que es literalmente lo que ve la
    transacción perdedora, cuyo SELECT corrió antes del commit de la ganadora—. Sin
    ese parche la función encuentra el balance en su primer SELECT, retorna temprano
    y el savepoint NUNCA se ejercita: el test pasaría vacío.

    Se ciega por FORMA del statement, no por número de llamada. Cegar "la llamada
    #1" sería correcto solo en SQLite: en Postgres,
    ``maintenance_lock_service.acquire_write_lock_shared`` corre ANTES y emite su
    propio ``SELECT pg_advisory_xact_lock_shared(...)``, así que la #1 sería el
    lock, el SELECT real devolvería el balance, la función retornaría temprano —
    y el test pasaría igual sin tocar el savepoint. Es el patrón exacto de
    "SQLite verde ≠ Postgres verde", y con ``-m postgres`` en CI ya no es teórico.
    """
    tid = sample_tenant.tenant_id
    producto = _product(tid, name="Arroz", stock_units=7)
    db_session.add(producto)
    await db_session.flush()

    ganador = InventoryBalance(
        tenant_id=tid, product_id=producto.id, current_qty=7, reserved_qty=0
    )
    db_session.add(ganador)
    await db_session.flush()

    real_execute = db_session.execute
    cegados = {"n": 0}

    def _es_select_de_balance(stmt: Any) -> bool:
        return InventoryBalance.__table__ in getattr(stmt, "froms", []) or (
            "inventory_balances" in str(stmt).lower() and str(stmt).lower().startswith("select")
        )

    async def _execute_ciego(*args: Any, **kw: Any) -> Any:
        result = await real_execute(*args, **kw)
        if args and _es_select_de_balance(args[0]) and cegados["n"] == 0:
            cegados["n"] += 1

            class _Vacio:
                @staticmethod
                def scalar_one_or_none() -> None:
                    return None

            return _Vacio()
        return result

    with patch.object(db_session, "execute", _execute_ciego):
        balance = await stock_service._get_or_create_balance(producto, tid, db_session)

    assert balance.id == ganador.id, "debió resolver al balance que ya existía"

    filas = (
        (
            await db_session.execute(
                select(InventoryBalance).where(InventoryBalance.product_id == producto.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(filas) == 1, "quedaron dos filas de balance para el mismo producto"


# ── API: 409 en vez de 500 cuando se pierde la carrera ───────────────────────


_PAYLOAD: dict[str, Any] = {
    "name": "Producto de prueba",
    "sale_price_ars": "150.00",
    "unit_cost_ars": "100.00",
    "stock_units": 10,
}


@pytest.mark.usefixtures("f5_indexes")
async def test_post_products_perdiendo_la_carrera_devuelve_409_no_500(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    mock_score_trigger: Any,
) -> None:
    """La request que pierde el TOCTOU debe ver el MISMO 409 que el pre-check.

    Se parchea ``_find_identity_matches`` para que no encuentre nada: es lo que ve
    quien consultó antes de que la otra transacción commiteara. Sin el guard, acá
    salía un IntegrityError → 500.
    """
    primero = {**_PAYLOAD, "sku": "CARRERA-1"}
    resp1 = await client.post("/api/v1/products", json=primero, headers=auth_headers)
    assert resp1.status_code == 201

    with patch(
        "app.api.v1.products._find_identity_matches",
        return_value=(None, None),
    ):
        segundo = {**_PAYLOAD, "name": "Otro nombre", "sku": "carrera-1"}
        resp2 = await client.post("/api/v1/products", json=segundo, headers=auth_headers)

    assert resp2.status_code == 409, f"esperaba 409, salió {resp2.status_code}"
    detail = resp2.json()["detail"]
    assert detail["code"] == "DUPLICATE_PRODUCT_IDENTITY"
    assert detail["existing_id"] == resp1.json()["id"]


@pytest.mark.usefixtures("f5_indexes")
async def test_patch_products_perdiendo_la_carrera_devuelve_409_no_500(
    client: AsyncClient, auth_headers: dict[str, Any], mock_score_trigger: Any
) -> None:
    """Igual que el POST, pero por el camino de los ``setattr`` dentro del guard."""
    ocupante = await client.post(
        "/api/v1/products", json={**_PAYLOAD, "sku": "OCUPADO-1"}, headers=auth_headers
    )
    assert ocupante.status_code == 201
    a_editar = await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "Editable", "sku": "LIBRE-1"},
        headers=auth_headers,
    )
    assert a_editar.status_code == 201

    with patch(
        "app.api.v1.products._find_identity_matches",
        return_value=(None, None),
    ):
        resp = await client.patch(
            f"/api/v1/products/{a_editar.json()['id']}",
            json={"sku": "ocupado-1"},
            headers=auth_headers,
        )

    assert resp.status_code == 409, f"esperaba 409, salió {resp.status_code}"
    assert resp.json()["detail"]["code"] == "DUPLICATE_PRODUCT_IDENTITY"


@pytest.mark.usefixtures("f5_indexes")
async def test_post_products_ambiguo_devuelve_los_dos_candidatos(
    client: AsyncClient, auth_headers: dict[str, Any], mock_score_trigger: Any
) -> None:
    """Barcode de uno y SKU de otro → IDENTITY_CONFLICT con AMBOS ids.

    Es el caso donde elegir uno sería inventar: el usuario tiene que ver los dos
    para decidir cuál corresponde.
    """
    a = await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "A", "barcode": "7790011110001"},
        headers=auth_headers,
    )
    b = await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "B", "sku": "SKU-B"},
        headers=auth_headers,
    )
    assert a.status_code == 201 and b.status_code == 201

    with patch(
        "app.api.v1.products._find_identity_matches",
        return_value=(None, None),
    ):
        resp = await client.post(
            "/api/v1/products",
            json={**_PAYLOAD, "name": "Mezcla", "barcode": "7790011110001", "sku": "SKU-B"},
            headers=auth_headers,
        )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "IDENTITY_CONFLICT"
    assert detail["barcode_product_id"] == a.json()["id"]
    assert detail["sku_product_id"] == b.json()["id"]


# ── Reactivación: el índice parcial vive sobre ``is_active`` ─────────────────
#
# Un producto dado de baja queda FUERA del índice único, así que su sku/barcode
# puede ser ocupado por otro. Reactivarlo lo vuelve a meter — con una clave que
# ahora tiene dueño. El PATCH no manda ``sku`` en ese request, así que la
# identidad hay que calcularla EFECTIVA (post-mutación), no leerla del body.


async def _crear_y_dar_de_baja(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict[str, Any],
    **identidad: Any,
) -> Product:
    resp = await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "Dado de baja", **identidad},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    producto = (
        await db_session.execute(
            select(Product).where(Product.id == uuid.UUID(resp.json()["id"]))
        )
    ).scalar_one()
    producto.is_active = False
    producto.deactivated_at = datetime.now(UTC)
    producto.deactivation_reason = DEACTIVATION_REASON_MANUAL
    await db_session.flush()
    return producto


@pytest.mark.usefixtures("f5_indexes")
async def test_reactivar_con_sku_ya_ocupado_devuelve_409_no_500(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    mock_score_trigger: Any,
) -> None:
    """Reactivar un producto cuyo SKU ocupó otro: 409, nunca 500."""
    baja = await _crear_y_dar_de_baja(
        client, db_session, auth_headers, sku="REACT-SKU"
    )
    ocupante = await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "Ocupante", "sku": "REACT-SKU"},
        headers=auth_headers,
    )
    assert ocupante.status_code == 201, "con el de baja fuera del índice, esto entra"

    resp = await client.patch(
        f"/api/v1/products/{baja.id}",
        json={"is_active": True},
        headers=auth_headers,
    )
    assert resp.status_code == 409, f"esperaba 409, salió {resp.status_code}"
    assert resp.json()["detail"]["code"] == "DUPLICATE_PRODUCT_IDENTITY"


@pytest.mark.usefixtures("f5_indexes")
async def test_reactivar_con_barcode_ya_ocupado_devuelve_409_no_500(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    mock_score_trigger: Any,
) -> None:
    """Misma historia por barcode: la clave no viene en el body igual."""
    baja = await _crear_y_dar_de_baja(
        client, db_session, auth_headers, barcode="7790011119991"
    )
    ocupante = await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "Ocupante", "barcode": "7790011119991"},
        headers=auth_headers,
    )
    assert ocupante.status_code == 201

    resp = await client.patch(
        f"/api/v1/products/{baja.id}",
        json={"is_active": True},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "DUPLICATE_PRODUCT_IDENTITY"


@pytest.mark.usefixtures("f5_indexes")
async def test_reactivacion_fallida_no_deja_rastro(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    mock_score_trigger: Any,
) -> None:
    """El 409 no puede dejar el producto medio-reactivado ni auditar un update.

    Tres cosas juntas porque son el MISMO invariante (el rechazo es total):
    sigue inactivo con su metadata de baja, no hay ``DATA_RECORD_UPDATED``, y la
    sesión queda usable para el request siguiente.
    """
    baja = await _crear_y_dar_de_baja(
        client, db_session, auth_headers, sku="REACT-RASTRO"
    )
    await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "Ocupante", "sku": "REACT-RASTRO"},
        headers=auth_headers,
    )

    resp = await client.patch(
        f"/api/v1/products/{baja.id}",
        json={"is_active": True},
        headers=auth_headers,
    )
    assert resp.status_code == 409

    await db_session.refresh(baja)
    assert baja.is_active is False
    assert baja.deactivated_at is not None, "la baja no se tocó"

    audits = (
        (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "DATA_RECORD_UPDATED"
                )
            )
        )
        .scalars()
        .all()
    )
    assert [a for a in audits if a.decision_data.get("record_id") == str(baja.id)] == []

    # La sesión sigue viva: un request posterior funciona con normalidad.
    otro = await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "Posterior", "sku": "REACT-POSTERIOR"},
        headers=auth_headers,
    )
    assert otro.status_code == 201


@pytest.mark.usefixtures("f5_indexes")
async def test_reactivar_sin_colision_funciona_y_limpia_la_baja(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    mock_score_trigger: Any,
) -> None:
    """Sin ocupante la reactivación pasa — y borra la metadata de baja.

    Sin este caso el test del 409 pasaría también con un pre-check que rechaza
    TODA reactivación.
    """
    baja = await _crear_y_dar_de_baja(
        client, db_session, auth_headers, sku="REACT-LIBRE"
    )

    resp = await client.patch(
        f"/api/v1/products/{baja.id}",
        json={"is_active": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200

    await db_session.refresh(baja)
    assert baja.is_active is True
    assert baja.deactivated_at is None, "reactivar borra la baja, no la deja colgada"
    assert baja.deactivation_reason is None


@pytest.mark.usefixtures("f5_indexes")
async def test_reactivar_cambiando_a_un_sku_libre_funciona(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    mock_score_trigger: Any,
) -> None:
    """Reactivar + mover el SKU al mismo tiempo: manda la clave NUEVA.

    Si la identidad efectiva se calculara mal (leyendo el sku viejo del objeto en
    vez del enviado), esto daría un 409 espurio contra el ocupante.
    """
    baja = await _crear_y_dar_de_baja(
        client, db_session, auth_headers, sku="REACT-VIEJO"
    )
    await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "Ocupante", "sku": "REACT-VIEJO"},
        headers=auth_headers,
    )

    resp = await client.patch(
        f"/api/v1/products/{baja.id}",
        json={"is_active": True, "sku": "REACT-NUEVO"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    await db_session.refresh(baja)
    assert baja.is_active is True
    assert baja.sku == "REACT-NUEVO"


@pytest.mark.usefixtures("f5_indexes")
async def test_reactivar_perdiendo_la_carrera_llega_al_guard_y_da_409(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    mock_score_trigger: Any,
) -> None:
    """El pre-check tapa el caso normal; acá se prueba la red de atrás.

    Con el pre-check cegado (lo que ve quien pierde la carrera TOCTOU), el UPDATE
    llega al índice único. El guard tiene que traducirlo a 409 — y para eso
    necesita la identidad EFECTIVA, porque el body no trae ``sku``.
    """
    baja = await _crear_y_dar_de_baja(
        client, db_session, auth_headers, sku="REACT-CARRERA"
    )
    await client.post(
        "/api/v1/products",
        json={**_PAYLOAD, "name": "Ocupante", "sku": "REACT-CARRERA"},
        headers=auth_headers,
    )

    with patch(
        "app.api.v1.products._find_identity_matches",
        return_value=(None, None),
    ):
        resp = await client.patch(
            f"/api/v1/products/{baja.id}",
            json={"is_active": True},
            headers=auth_headers,
        )

    assert resp.status_code == 409, f"esperaba 409, salió {resp.status_code}"
    assert resp.json()["detail"]["code"] == "DUPLICATE_PRODUCT_IDENTITY"
