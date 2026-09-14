"""La apertura sin fecha pertenece al primer día registrado, sin afectar caja."""

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

import app.application.services.ingestion_import_service as importer
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.transaction import ExpenseEntry, SaleEntry


def _catalog(mode="contexts"):
    row = {"producto": "Vela", "stock": "10", "costo": "100"}
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock" if mode == "flat" else "mixed",
        "has_producto": True,
        "stock_detectado": [row],
    }
    if mode == "contexts":
        row["__context__"] = "catalog"
        summary["mapping_contexts"] = [{"context_id": "catalog", "entity_type": "product"}]
    return summary


def _sheet(summary, cid, entity, rows):
    bucket = "ventas_detectadas" if entity == "sale" else "gastos_detectados"
    summary.setdefault(bucket, []).extend(dict(row, __context__=cid) for row in rows)
    summary["mapping_contexts"].insert(0, {"context_id": cid, "entity_type": entity})


async def _movement(session):
    await session.flush()
    return (await session.execute(select(InventoryMovement))).scalar_one()


@pytest.mark.parametrize("mode", ["flat", "contexts", "legacy"])
@pytest.mark.parametrize("book", [SaleEntry, ExpenseEntry])
async def test_catalogo_posterior_usa_historial_vigente_del_tenant(
    db_session, sample_tenant, second_tenant, mode, book
):
    tid = sample_tenant.tenant_id
    for tenant, day, provenance, void in [
        (tid, datetime(2025, 1, 3, 18), "REAL", False),
        (second_tenant.tenant_id, datetime(2020, 1, 1), "REAL", False),
        (tid, datetime(2021, 1, 1), "DEMO", False),
        (tid, datetime(2022, 1, 1), "REAL", True),
    ]:
        kwargs = {"category": "OTHER", "description": "Gasto"} if book is ExpenseEntry else {}
        db_session.add(
            book(
                tenant_id=tenant,
                transaction_date=day,
                amount=Decimal("100"),
                provenance=provenance,
                voided_at=datetime(2026, 1, 1) if void else None,
                void_reason="USER_CANCELLED" if void else None,
                **kwargs,
            )
        )
    await db_session.flush()
    await importer.insert_confirmed_data(db_session, tid, _catalog(mode), {"productos": True})
    movement = await _movement(db_session)
    assert movement.occurred_at.replace(tzinfo=None) == datetime(2025, 1, 3)
    assert movement.movement_type == "adjustment"
    assert movement.qty == 10
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(expenses) == (4 if book is ExpenseEntry else 0)


@pytest.mark.parametrize("mode", ["flat", "contexts", "legacy"])
@pytest.mark.parametrize("treatment", ["opening_balance", "purchase"])
async def test_sin_fecha_conserva_fallback_y_compra_conserva_gasto(
    db_session, sample_tenant, monkeypatch, mode, treatment
):
    today = datetime(2026, 9, 14, 15, 30)
    monkeypatch.setattr(importer, "now_ar_naive", lambda: today)
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _catalog(mode),
        {"productos": True},
        stock_treatment=treatment,
    )
    movement = await _movement(db_session)
    assert movement.occurred_at.replace(tzinfo=None) == today
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(expenses) == (1 if treatment == "purchase" else 0)
    if expenses:
        assert expenses[0].transaction_date == today
        assert expenses[0].amount == Decimal("1000")


@pytest.mark.parametrize("purchase", [False, True])
@pytest.mark.parametrize("previous", [None, datetime(2024, 12, 20), datetime(2025, 3, 1)])
async def test_momento_cero_lee_todas_las_hojas_incluidas_y_su_mapeo(
    db_session, sample_tenant, monkeypatch, purchase, previous
):
    today = datetime(2026, 9, 14, 15, 30)
    monkeypatch.setattr(importer, "now_ar_naive", lambda: today)
    if previous is not None:
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id, transaction_date=previous, amount=Decimal("50")
            )
        )
        await db_session.flush()
    summary = _catalog()
    _sheet(
        summary,
        "sales",
        "sale",
        [
            {"dia real": "2025-02-15", "producto": "Vela", "monto": "200"},
            {"dia real": "2025-01-02", "producto": "Vela", "monto": "200"},
        ],
    )
    _sheet(
        summary,
        "expenses",
        "expense",
        [
            {"dia real": "2025-01-01", "descripcion": "Alquiler", "monto": "100"},
        ],
    )
    _sheet(summary, "excluded", "sale", [{"fecha": "2019-01-01", "monto": "50"}])
    result = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True, "ventas": True, "gastos": True},
        context_confirmed={"catalog": True, "sales": True, "expenses": True, "excluded": False},
        context_mappings={
            "sales": {
                "dia real": "transaction_date",
                "producto": "product_name",
                "monto": "amount",
            },
            "expenses": {"dia real": "expense_date", "monto": "amount"},
        },
        stock_treatment="purchase" if purchase else "opening_balance",
    )
    movement = await _movement(db_session)
    opening = min(previous, datetime(2025, 1, 1)) if previous else datetime(2025, 1, 1)
    assert movement.occurred_at.replace(tzinfo=None) == (today if purchase else opening)
    assert movement.movement_type == ("purchase" if purchase else "adjustment")
    assert result["ventas"] == 2
    if not purchase:
        assert result["historial_sin_fecha"] == 0
        assert result["historial_insuficiente"] == 0
    sales = (await db_session.execute(select(SaleEntry))).scalars().all()
    expected_dates = {datetime(2025, 1, 2), datetime(2025, 2, 15)}
    if previous:
        expected_dates.add(previous)
    assert {s.transaction_date for s in sales} == expected_dates
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(expenses) == (2 if purchase else 1)


async def test_fecha_ignorada_o_invalida_no_se_usa_como_momento_cero(
    db_session, sample_tenant, monkeypatch
):
    today = datetime(2026, 9, 14, 15, 30)
    monkeypatch.setattr(importer, "now_ar_naive", lambda: today)
    summary = _catalog()
    _sheet(summary, "sales", "sale", [{"fecha": "2019-01-01", "monto": "50"}])
    _sheet(summary, "expenses", "expense", [{"fecha": "no es fecha", "monto": "50"}])
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True, "ventas": True, "gastos": True},
        context_mappings={"sales": {"fecha": "ignore"}},
    )
    assert (await _movement(db_session)).occurred_at.replace(tzinfo=None) == today


@pytest.mark.parametrize("mode", ["flat", "contexts", "legacy"])
async def test_apertura_actualiza_producto_existente_y_respeta_fecha_explicita(
    db_session, sample_tenant, mode
):
    db_session.add(
        Product(
            tenant_id=sample_tenant.tenant_id,
            name="Vela",
            stock_units=5,
            sale_price_ars=Decimal("200"),
        )
    )
    db_session.add(
        SaleEntry(
            tenant_id=sample_tenant.tenant_id,
            transaction_date=datetime(2025, 1, 1),
            amount=Decimal("100"),
        )
    )
    await db_session.flush()
    summary = _catalog(mode)
    summary["stock_detectado"][0]["fecha_adquisicion"] = "2025-02-10"
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
    )
    movement = await _movement(db_session)
    assert movement.qty == 5
    assert movement.occurred_at.replace(tzinfo=None) == datetime(2025, 2, 10)
    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.stock_units == 10
    assert (await db_session.execute(select(ExpenseEntry))).scalars().all() == []
