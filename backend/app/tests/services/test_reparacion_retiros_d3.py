"""D·3 — los retiros viejos (gasto con ``profit_withdrawal``) pasan a capital.

Se protege: que sólo se tocan los gastos con la marca; que el resultado sube el
importe y la caja no cambia (por día y medio, arqueo y proyección); que todo va en
una transacción por tenant; y que revertir es de a pares indivisibles.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reparacion_retiros_service as svc
from app.application.services.facts_provider import margenes_del_tenant
from app.application.services.facts_service import Period
from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests.api.v1.test_owner_capital import _gasto_retiro_viejo

HOY = today_ar()
MES = Period.mes_cerrado(today=HOY)


def _a_las(d: date) -> datetime:
    return datetime.combine(d, time(12))


def _venta(tenant: Tenant, monto: str, d: date, costo: str) -> SaleEntry:
    return SaleEntry(
        tenant_id=tenant.tenant_id, amount=Decimal(monto), transaction_date=_a_las(d),
        payment_method="cash", cost_ars=Decimal(costo), cost_unit_ars=Decimal(costo),
        cost_confirmed=True, cost_origin="compra",
    )


def _gasto(tenant: Tenant, monto: str, d: date, categoria: str = "RENT") -> ExpenseEntry:
    return ExpenseEntry(
        tenant_id=tenant.tenant_id, amount=Decimal(monto), category=categoria,
        expense_type="OPEX", transaction_date=_a_las(d), description="Alquiler",
        is_recurring=False, payment_method="transfer",
    )


async def _mes_con_retiro(
    db: AsyncSession, tenant: Tenant, *, con_alquiler: bool = True
) -> ExpenseEntry:
    """Mes cerrado: ventas $10.000 con costo $4.000, alquiler $1.000 y un retiro
    viejo de $2.000 en efectivo."""
    dia = MES.start
    db.add(_venta(tenant, "10000", dia, "4000"))
    if con_alquiler:
        db.add(_gasto(tenant, "1000", dia))
    viejo = _gasto_retiro_viejo(tenant, Decimal("2000"), dia, "cash")
    db.add(viejo)
    await db.commit()
    return viejo


async def _cuenta(db: AsyncSession, modelo: Any, *cond: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(modelo).where(*cond)) or 0)


# ── Análisis ─────────────────────────────────────────────────────────────────


async def test_analizar_solo_lee_y_toma_solo_la_marca(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    viejo = await _mes_con_retiro(db_session, sample_tenant)
    # Un sueldo PAYROLL sin la marca no es un retiro, aunque lo parezca.
    db_session.add(_gasto(sample_tenant, "3000", MES.start, "PAYROLL"))
    await db_session.commit()

    r = await svc.analizar(db_session, sample_tenant.tenant_id)

    assert [c.expense_id for c in r.candidatos] == [viejo.id]
    assert r.monto == Decimal("2000")
    assert r.por_medio() == {"cash": Decimal("2000")}
    assert r.efecto == {}
    assert not db_session.new and not db_session.dirty
    assert await _cuenta(db_session, OwnerCapitalMovement) == 0


async def test_exclusiones_con_motivo(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    raro = _gasto_retiro_viejo(sample_tenant, Decimal("500"), MES.start, "cheque")
    otro_tenant = _gasto_retiro_viejo(second_tenant, Decimal("700"), MES.start, "cash")
    migrado = _gasto_retiro_viejo(sample_tenant, Decimal("900"), MES.start, "cash")
    db_session.add_all([raro, otro_tenant, migrado])
    await db_session.flush()
    db_session.add(OwnerCapitalMovement(
        tenant_id=sample_tenant.tenant_id, kind="retiro", form="dinero", amount_ars=900,
        occurred_on=MES.start, payment_method="cash", source_type="migrado_de_gasto",
        source_ref=str(migrado.id),
    ))
    await db_session.commit()

    r = await svc.analizar(db_session, sample_tenant.tenant_id)

    assert r.candidatos == []
    assert r.excluidos == {svc.MOTIVO_MEDIO: 1, svc.MOTIVO_YA_MIGRADO: 1}


async def test_una_fecha_futura_se_excluye_y_no_bloquea_al_resto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un retiro viejo con fecha posterior a hoy (antes del validador) no se
    convierte: persistiría una fecha futura y la proyección lo vería distinto."""
    futuro = _gasto_retiro_viejo(sample_tenant, Decimal("800"), HOY + timedelta(days=3), "cash")
    normal = _gasto_retiro_viejo(sample_tenant, Decimal("300"), HOY - timedelta(days=3), "cash")
    db_session.add_all([futuro, normal])
    await db_session.commit()

    r = await svc.reparar(db_session, sample_tenant.tenant_id, aplicar=True)
    await db_session.commit()

    assert [c.expense_id for c in r.candidatos] == [normal.id]
    assert r.excluidos == {svc.MOTIVO_FECHA_FUTURA: 1}
    await db_session.refresh(futuro)
    assert futuro.voided_at is None


async def test_filtro_de_periodo(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    viejo = _gasto_retiro_viejo(sample_tenant, Decimal("100"), date(2026, 1, 10), "cash")
    nuevo = _gasto_retiro_viejo(sample_tenant, Decimal("200"), date(2026, 5, 10), "cash")
    db_session.add_all([viejo, nuevo])
    await db_session.commit()

    r = await svc.analizar(db_session, sample_tenant.tenant_id, date(2026, 5, 1), None)

    assert [c.expense_id for c in r.candidatos] == [nuevo.id]


# ── Reparar ──────────────────────────────────────────────────────────────────


async def test_simulacion_no_deja_cambios(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    viejo = await _mes_con_retiro(db_session, sample_tenant)
    r = await svc.reparar(db_session, sample_tenant.tenant_id, aplicar=False)
    assert r.run_id is not None and r.efecto["antes"] != r.efecto["despues"]
    await db_session.rollback()

    assert await _cuenta(db_session, OwnerCapitalMovement) == 0
    assert await _cuenta(db_session, DataRepairRun) == 0
    await db_session.refresh(viejo)
    assert viejo.voided_at is None


async def test_apply_convierte_y_sube_el_resultado_sin_tocar_la_caja(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    viejo = await _mes_con_retiro(db_session, sample_tenant)
    neto_antes = (await margenes_del_tenant(db_session, tid)).neto  # type: ignore[union-attr]
    caja_antes = await svc.foto_de_caja(db_session, tid, {_a_las(MES.start).date()})

    r = await svc.reparar(db_session, tid, aplicar=True)
    await db_session.commit()

    neto = (await margenes_del_tenant(db_session, tid)).neto  # type: ignore[union-attr]
    # (10.000 − 4.000 − 1.000 − 2.000) / 10.000 = 30% → sin el retiro, 50%:
    # sube importe / ventas × 100 = 20 puntos.
    assert (neto_antes.value, neto.value) == (30.0, 50.0)
    assert Decimal(r.efecto["despues"]["resultado_ars"]) - Decimal(
        r.efecto["antes"]["resultado_ars"]
    ) == Decimal("2000")
    await db_session.refresh(viejo)
    assert (viejo.voided_at is not None, viejo.voided_by_repair_run_id) == (True, r.run_id)
    cap = (await db_session.execute(select(OwnerCapitalMovement))).scalar_one()
    assert (cap.kind, cap.form, cap.amount_ars, cap.payment_method, cap.occurred_on) == (
        "retiro", "dinero", Decimal("2000.00"), "cash", viejo.transaction_date.date(),
    )
    assert (cap.source_type, cap.source_ref) == ("migrado_de_gasto", str(viejo.id))
    caja_despues = await svc.foto_de_caja(db_session, tid, {viejo.transaction_date.date()})
    assert svc.diferencias_de_caja(caja_antes, caja_despues) == []
    assert caja_antes.proyeccion is not None
    assert await _cuenta(
        db_session, DecisionAuditLog,
        DecisionAuditLog.decision_type == "REPAIR_PROFIT_WITHDRAWAL_TO_CAPITAL",
    ) == 1


async def test_la_caja_queda_igual_por_dia_medio_arqueo_y_proyeccion(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Retiros de hoy (arqueo), de hace 5 días (proyección) y de hace 90 (fuera de
    la ventana de 30): ninguno mueve la caja."""
    tid = sample_tenant.tenant_id
    dias = {HOY, HOY - timedelta(days=5), HOY - timedelta(days=90)}
    for d in (20, 10, 1):
        db_session.add(_venta(sample_tenant, "10000", HOY - timedelta(days=d), "1"))
    for dia, medio in zip(sorted(dias), ("cash", "transfer", "cash"), strict=True):
        db_session.add(_gasto_retiro_viejo(sample_tenant, Decimal("3000"), dia, medio))
    await db_session.commit()
    antes = await svc.foto_de_caja(db_session, tid, dias)

    await svc.reparar(db_session, tid, aplicar=True)
    await db_session.commit()

    despues = await svc.foto_de_caja(db_session, tid, dias)
    assert despues == antes
    assert len(antes.salidas) == 3 and antes.proyeccion is not None


async def test_si_era_el_unico_gasto_el_neto_queda_sin_medir(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _mes_con_retiro(db_session, sample_tenant, con_alquiler=False)
    r = await svc.reparar(db_session, tid, aplicar=True)
    await db_session.commit()

    assert r.efecto["antes"]["margen_neto_pct"] == 40.0
    assert r.efecto["despues"]["margen_neto_pct"] is None
    assert r.efecto["despues"]["margen_neto_estado"] == "ausente"
    assert "gastos" in (r.efecto["despues"]["margen_neto_motivo"] or "")


async def test_reejecutar_no_duplica(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    tid = sample_tenant.tenant_id
    await _mes_con_retiro(db_session, sample_tenant)
    await svc.reparar(db_session, tid, aplicar=True)
    await db_session.commit()

    segunda = await svc.reparar(db_session, tid, aplicar=True)
    await db_session.commit()

    assert (segunda.candidatos, segunda.run_id) == ([], None)
    assert await _cuenta(db_session, OwnerCapitalMovement) == 1


async def test_si_la_caja_cambia_no_se_confirma_nada(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    viejo = await _mes_con_retiro(db_session, sample_tenant)
    real = svc.foto_de_caja
    llamadas = 0

    async def _cambiada(session: AsyncSession, tenant_id: Any, dias: Any) -> svc.FotoDeCaja:
        nonlocal llamadas
        llamadas += 1
        foto = await real(session, tenant_id, dias)
        if llamadas == 2:
            return svc.FotoDeCaja({**foto.salidas, (HOY, "qr"): Decimal("1")}, foto.arqueo,
                                  foto.proyeccion)
        return foto

    monkeypatch.setattr(svc, "foto_de_caja", _cambiada)
    with pytest.raises(svc.CajaCambioError):
        await svc.reparar(db_session, tid, aplicar=True)
    await db_session.rollback()

    assert await _cuenta(db_session, OwnerCapitalMovement) == 0
    await db_session.refresh(viejo)
    assert viejo.voided_at is None


def test_una_diferencia_compensada_entre_medios_se_detecta() -> None:
    dia = date(2026, 9, 1)
    antes = svc.FotoDeCaja({(dia, "cash"): Decimal("100")}, {}, None)
    despues = svc.FotoDeCaja({(dia, "transfer"): Decimal("100")}, {}, None)
    assert len(svc.diferencias_de_caja(antes, despues)) == 2


# ── Revertir ─────────────────────────────────────────────────────────────────


async def _dos_retiros_aplicados(
    db: AsyncSession, tenant: Tenant
) -> tuple[ExpenseEntry, ExpenseEntry, Any]:
    a = _gasto_retiro_viejo(tenant, Decimal("1000"), MES.start, "cash")
    b = _gasto_retiro_viejo(tenant, Decimal("2000"), MES.start + timedelta(days=1), "transfer")
    db.add_all([a, b])
    await db.commit()
    r = await svc.reparar(db, tenant.tenant_id, aplicar=True)
    await db.commit()
    return a, b, r.run_id


async def test_revertir_deja_todo_como_antes(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    a, b, run_id = await _dos_retiros_aplicados(db_session, sample_tenant)
    res = await svc.revertir(db_session, run_id)
    await db_session.commit()

    assert res.completa and len(res.revertidos) == 2
    for g in (a, b):
        await db_session.refresh(g)
        assert g.voided_at is None and g.voided_by_repair_run_id is None
    assert await _cuenta(
        db_session, OwnerCapitalMovement, OwnerCapitalMovement.voided_at.is_(None)
    ) == 0
    run = await db_session.get(DataRepairRun, run_id)
    assert run is not None and run.status == "REVERTED"


@pytest.mark.parametrize("cambio", ["capital_editado", "capital_anulado", "gasto_revivido"])
async def test_un_par_cambiado_se_saltea_entero_y_se_reintenta(
    db_session: AsyncSession, sample_tenant: Tenant, cambio: str
) -> None:
    a, b, run_id = await _dos_retiros_aplicados(db_session, sample_tenant)
    cap_a = (
        await db_session.execute(
            select(OwnerCapitalMovement).where(OwnerCapitalMovement.source_ref == str(a.id))
        )
    ).scalar_one()
    original = cap_a.amount_ars
    if cambio == "capital_editado":
        cap_a.amount_ars = Decimal("1500")
    elif cambio == "capital_anulado":
        cap_a.voided_at = datetime.now()
    else:
        a.voided_at = None
        a.voided_by_repair_run_id = None
    await db_session.commit()

    res = await svc.revertir(db_session, run_id)
    await db_session.commit()

    assert res.revertidos == [str(b.id)]
    assert list(res.pendientes) == [str(a.id)]
    # Si un usuario anuló el retiro migrado no se reintenta: revivir el gasto
    # desharía su anulación.
    assert bool(res.reintentables) is (cambio != "capital_anulado")
    run = await db_session.get(DataRepairRun, run_id)
    assert run is not None and run.status == "APPLIED"
    assert run.details_json is not None and run.details_json["pendientes"] == res.pendientes
    # El par salteado quedó entero: ni el gasto revivido con el capital vivo (doble
    # salida) ni los dos anulados (el retiro desaparece de la caja).
    await db_session.refresh(a)
    await db_session.refresh(cap_a)
    if cambio == "capital_editado":
        assert a.voided_at is not None and cap_a.voided_at is None
        # Vuelve a coincidir: el segundo intento revierte sólo el pendiente.
        cap_a.amount_ars = original
        await db_session.commit()
        otra = await svc.revertir(db_session, run_id)
        await db_session.commit()
        assert (otra.revertidos, otra.completa) == ([str(a.id)], True)
        await db_session.refresh(run)
        assert run.status == "REVERTED"


# ── Script ───────────────────────────────────────────────────────────────────


def test_el_script_solo_aplica_de_a_un_negocio() -> None:
    import importlib.util  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    ruta = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "reparar_retiros_a_capital.py"
    spec = importlib.util.spec_from_file_location("reparar_retiros_a_capital", ruta)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def args(*a: str) -> argparse.Namespace:
        ns: argparse.Namespace = mod.parser().parse_args(list(a))
        return ns

    assert mod.validar(args("--all")) is None
    assert mod.validar(args("--email", "x@y.com")) is None
    assert mod.validar(args("--email", "x@y.com", "--apply")) is None
    assert mod.validar(args("--all", "--apply")) is not None
    assert mod.validar(args("--all", "--simular")) is not None
    assert mod.validar(args("--revert", "x", "--apply")) is not None
    assert mod.validar(args("--revert", "x", "--desde", "2026-01-01")) is not None
    with pytest.raises(SystemExit):
        mod.parser().parse_args(["--email", "x", "--apply", "--simular"])
