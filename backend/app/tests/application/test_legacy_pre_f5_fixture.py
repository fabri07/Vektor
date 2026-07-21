"""La fixture ``legacy_pre_f5_schema`` no puede contaminar al resto del worker.

El schema de test es ``scope="session"`` (una vez por worker de xdist, ver conftest),
así que un ``DROP INDEX`` que sobreviva al test dejaría a TODOS los tests siguientes
de ese worker corriendo contra el esquema pre-F5 — verdes por ausencia de índice, no
por correctos. Es el modo de falla más caro posible: silencioso y dependiente del
orden.

La restauración la da el rollback de la transacción externa de ``db_session`` (en
SQLite el DDL es transaccional). Estos dos tests fijan esa garantía, que si no queda
como suposición implícita de la fixture.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_INDEXES = (
    "uq_products_tenant_barcode_norm",
    "uq_products_tenant_sku_norm",
    "uq_inventory_balances_tenant_product",
)


async def _indices_presentes(session: AsyncSession) -> set[str]:
    rows = (
        await session.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND name IN (:a, :b, :c)"),
            {"a": _INDEXES[0], "b": _INDEXES[1], "c": _INDEXES[2]},
        )
    ).scalars()
    return set(rows)


async def test_default_del_suite_es_post_f5(db_session: AsyncSession) -> None:
    """Sin pedir nada, los tres uniques están: el default es el esquema de producción."""
    assert await _indices_presentes(db_session) == set(_INDEXES)


async def test_legacy_pre_f5_schema_dropea_los_tres(
    db_session: AsyncSession, legacy_pre_f5_schema: None
) -> None:
    assert await _indices_presentes(db_session) == set()


async def test_legacy_pre_f5_schema_no_contamina(db_session: AsyncSession) -> None:
    """Corre después del anterior en el mismo worker y ve los índices de vuelta.

    Vale aunque pytest reordene: cualquier test que pida ``db_session`` sin la
    fixture legacy tiene que ver los tres. Si el rollback dejara de restaurarlos,
    este test (y varios otros) se caen — no queda en silencio.
    """
    assert await _indices_presentes(db_session) == set(_INDEXES)
