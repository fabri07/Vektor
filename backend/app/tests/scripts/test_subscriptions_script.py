"""Tests de ``scripts/subscriptions.py`` — la activación manual del dueño.

Mismo patrón que ``test_access_requests_script.py``: el módulo se carga por
ruta de archivo (``scripts/`` no es un paquete) y los handlers se invocan
directo con la sesión del test — ``main()`` no es testeable sin una base real,
pero la guardia del ``--apply`` vive en los handlers.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.tenant import PlanDefinition, Subscription, Tenant
from app.persistence.models.user import User
from app.utils.security import hash_password

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module() -> Any:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "subscriptions_script", _SCRIPTS_DIR / "subscriptions.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod() -> Any:
    return _load_module()


@pytest_asyncio.fixture
async def catalogo(db_session: AsyncSession) -> None:
    """Las 4 filas del catálogo — en SQLite `create_all` no corre el seed de
    la migración, así que los tests lo siembran a mano."""
    ahora = datetime.now(UTC)
    filas = [
        ("esencial", "Esencial", 1, 20, 5, 1, "12.00", "18500.00"),
        ("control", "Control", 3, 70, 25, 7, "27.00", "41500.00"),
        ("direccion", "Dirección", 5, 180, 75, 18, "59.00", "90500.00"),
    ]
    for plan_code, nombre, seats, ia, imports_, reads, usd, ars in filas:
        db_session.add(
            PlanDefinition(
                plan_code=plan_code,
                display_name=nombre,
                seats_included=seats,
                ia_queries_per_month=ia,
                imports_per_month=imports_,
                photo_pdf_reads_per_month=reads,
                price_usd_reference=usd,
                price_ars_reference=ars,
                is_offered=True,
                created_at=ahora,
                updated_at=ahora,
            )
        )
    await db_session.commit()


@pytest_asyncio.fixture
async def trial(db_session: AsyncSession, catalogo: None) -> tuple[Tenant, Subscription]:
    tenant = Tenant(
        tenant_id=uuid.uuid4(), legal_name="Kiosco Trial", display_name="Kiosco Trial"
    )
    db_session.add(tenant)
    await db_session.flush()
    db_session.add(
        User(
            user_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            email="duenio@trial.example.com",
            full_name="Ana",
            password_hash=hash_password("Secure123"),
            role_code="OWNER",
        )
    )
    ahora = datetime.now(UTC)
    sub = Subscription(
        subscription_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        plan_code="esencial",
        status="TRIAL",
        seats_included=1,
        trial_ends_at=ahora + timedelta(days=14),
    )
    db_session.add(sub)
    await db_session.commit()
    return tenant, sub


async def _contar(session: AsyncSession, modelo: type) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(modelo))).scalar_one()
    )


# ── show ─────────────────────────────────────────────────────────────────────


async def test_show_de_tenant_inexistente(mod: Any, db_session: AsyncSession) -> None:
    args = mod.parse_args(["show", str(uuid.uuid4())])
    assert await mod.cmd_show(db_session, args) == 1


async def test_show_por_email_encuentra_el_tenant(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription], capsys: Any
) -> None:
    args = mod.parse_args(["show", "duenio@trial.example.com"])
    assert await mod.cmd_show(db_session, args) == 0
    assert "TRIAL" in capsys.readouterr().out


# ── activate ─────────────────────────────────────────────────────────────────


def _args_activate(tenant_id: uuid.UUID, *, reference: str = "MP-1", apply: bool = False) -> Any:
    mod = _load_module()
    argv = [
        "activate", str(tenant_id),
        "--plan", "control",
        "--price-usd", "27",
        "--price-ars", "41500",
        "--reference", reference,
    ]
    if apply:
        argv.append("--apply")
    return mod.parse_args(argv)


async def test_dry_run_de_activate_no_escribe(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    tenant, sub = trial
    args = _args_activate(tenant.tenant_id, apply=False)
    assert await mod.cmd_activate(db_session, args) == 0

    await db_session.refresh(sub)
    assert sub.status == "TRIAL"
    assert sub.plan_code == "esencial"
    assert await _contar(db_session, DecisionAuditLog) == 0


async def test_activate_con_apply_otorga_cupos_y_precio_del_plan(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    tenant, sub = trial
    args = _args_activate(tenant.tenant_id, apply=True)
    assert await mod.cmd_activate(db_session, args) == 0

    await db_session.refresh(sub)
    assert sub.status == "ACTIVE"
    assert sub.plan_code == "control"
    assert sub.seats_included == 3
    assert sub.granted_ia_queries_per_month == 70
    assert sub.granted_imports_per_month == 25
    assert sub.granted_photo_pdf_reads_per_month == 7
    assert str(sub.plan_price_usd_reference) == "27.00"
    assert sub.current_period_start is not None
    assert sub.current_period_end is not None

    auditorias = (
        await db_session.execute(select(DecisionAuditLog))
    ).scalars().all()
    assert len(auditorias) == 1
    assert auditorias[0].decision_type == "SUBSCRIPTION_PAYMENT_APPLIED"


async def test_repetir_la_misma_referencia_no_vuelve_a_extender(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    tenant, sub = trial
    await mod.cmd_activate(db_session, _args_activate(tenant.tenant_id, apply=True))
    await db_session.refresh(sub)
    fin_original = sub.current_period_end

    # Repetir con la MISMA referencia — no debería tocar nada más.
    await mod.cmd_activate(db_session, _args_activate(tenant.tenant_id, apply=True))
    await db_session.refresh(sub)
    assert sub.current_period_end == fin_original
    assert await _contar(db_session, DecisionAuditLog) == 1  # no un segundo registro


async def test_plan_fuera_del_catalogo_asignable_es_indecible_en_la_cli(mod: Any) -> None:
    """`--plan` sale de `AssignablePlan`: `free`/`premium`/`FREE` son indecibles."""
    with pytest.raises(SystemExit):
        mod.parse_args(
            ["activate", str(uuid.uuid4()), "--plan", "FREE", "--price-usd", "0",
             "--price-ars", "0", "--reference", "x"]
        )


# ── set-status ───────────────────────────────────────────────────────────────


async def test_set_status_a_read_only_con_apply(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    tenant, sub = trial
    args = mod.parse_args(["set-status", str(tenant.tenant_id), "--status", "READ_ONLY", "--apply"])
    assert await mod.cmd_set_status(db_session, args) == 0

    await db_session.refresh(sub)
    assert sub.status == "READ_ONLY"


async def test_set_status_a_grace_setea_grace_ends_at(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    tenant, sub = trial
    args = mod.parse_args(["set-status", str(tenant.tenant_id), "--status", "GRACE", "--apply"])
    assert await mod.cmd_set_status(db_session, args) == 0

    await db_session.refresh(sub)
    assert sub.status == "GRACE"
    assert sub.grace_ends_at is not None


# ── expire-due ───────────────────────────────────────────────────────────────


async def test_expire_due_pasa_trial_vencida_a_read_only(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    tenant, sub = trial
    sub.trial_ends_at = datetime.now(UTC) - timedelta(days=1)  # ya venció
    await db_session.commit()

    args = mod.parse_args(["expire-due", "--apply"])
    assert await mod.cmd_expire_due(db_session, args) == 0

    await db_session.refresh(sub)
    assert sub.status == "READ_ONLY"


async def test_expire_due_no_toca_un_free_legado_con_periodo_viejo(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    """El seed de demo acuña FREE/ACTIVE con período a 30 días. Pasarlo a
    GRACE terminaba dejando en solo lectura una cuenta sin ciclo comercial."""
    _, sub = trial
    sub.plan_code = "FREE"
    sub.status = "ACTIVE"
    sub.current_period_start = datetime.now(UTC) - timedelta(days=90)
    sub.current_period_end = datetime.now(UTC) - timedelta(days=60)
    await db_session.commit()

    args = mod.parse_args(["expire-due", "--apply"])
    assert await mod.cmd_expire_due(db_session, args) == 0

    await db_session.refresh(sub)
    assert sub.status == "ACTIVE"


async def test_expire_due_pasa_un_plan_pago_vencido_a_gracia(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    _, sub = trial
    sub.status = "ACTIVE"
    sub.current_period_start = datetime.now(UTC) - timedelta(days=32)
    sub.current_period_end = datetime.now(UTC) - timedelta(days=2)
    await db_session.commit()

    args = mod.parse_args(["expire-due", "--apply"])
    assert await mod.cmd_expire_due(db_session, args) == 0

    await db_session.refresh(sub)
    assert sub.status == "GRACE"


async def test_expire_due_dry_run_no_toca_nada(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    tenant, sub = trial
    sub.trial_ends_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()

    args = mod.parse_args(["expire-due"])
    assert await mod.cmd_expire_due(db_session, args) == 0

    await db_session.refresh(sub)
    assert sub.status == "TRIAL"  # sin --apply, no cambia


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_apply_es_false_por_defecto_en_todo_comando_mutante(mod: Any) -> None:
    tenant_id = str(uuid.uuid4())
    invocaciones = (
        ["activate", tenant_id, "--plan", "control", "--price-usd", "1",
         "--price-ars", "1", "--reference", "x"],
        ["set-status", tenant_id, "--status", "READ_ONLY"],
        ["expire-due"],
    )
    for argv in invocaciones:
        assert mod.parse_args(argv).apply is False, argv


# ── diagnose ─────────────────────────────────────────────────────────────────


async def test_diagnose_encuentra_un_tenant_sin_suscripcion_y_no_escribe(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription],
    capsys: pytest.CaptureFixture[str],
) -> None:
    huerfano = Tenant(tenant_id=uuid.uuid4(), legal_name="Huérfano", display_name="Huérfano")
    db_session.add(huerfano)
    await db_session.commit()
    antes = await _contar(db_session, Subscription)

    assert await mod.cmd_diagnose(db_session, mod.parse_args(["diagnose"])) == 1

    assert str(huerfano.tenant_id) in capsys.readouterr().out
    assert await _contar(db_session, Subscription) == antes


# ── Bloque D: renew / change-plan / cancel / reparaciones ────────────────────


def _sin_zona(valor: datetime | None) -> datetime:
    """SQLite devuelve naive lo que se guardó con zona (Postgres no)."""
    assert valor is not None
    return valor.replace(tzinfo=None)


def _args_cobro(comando: str, tenant_id: uuid.UUID, ref: str, *extra: str) -> Any:
    argv = [comando, str(tenant_id), "--price-usd", "27", "--price-ars", "41500",
            "--reference", ref, "--apply", *extra]
    return _load_module().parse_args(argv)


async def test_activate_sobre_una_cuenta_ya_activa_manda_a_renew(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription], capsys: Any
) -> None:
    tenant, sub = trial
    assert await mod.cmd_activate(db_session, _args_activate(tenant.tenant_id, apply=True)) == 0
    await db_session.refresh(sub)
    fin = sub.current_period_end

    otra = _args_activate(tenant.tenant_id, reference="MP-2", apply=True)
    assert await mod.cmd_activate(db_session, otra) == 1
    assert "USE_RENEW" in capsys.readouterr().out
    await db_session.refresh(sub)
    assert _sin_zona(sub.current_period_end) == _sin_zona(fin)  # no se pisó


async def test_renew_por_el_script_otorga_el_periodo_siguiente(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    tenant, sub = trial
    await mod.cmd_activate(db_session, _args_activate(tenant.tenant_id, apply=True))
    await db_session.refresh(sub)
    fin = sub.current_period_end

    assert await mod.cmd_renew(db_session, _args_cobro("renew", tenant.tenant_id, "MP-2")) == 0

    await db_session.refresh(sub)
    assert _sin_zona(sub.current_period_end) == _sin_zona(fin)
    assert sub.next_period_end is not None
    assert _sin_zona(sub.next_period_end) > _sin_zona(fin)


async def test_varios_meses_en_un_pago_se_rechaza(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription], capsys: Any
) -> None:
    tenant, sub = trial
    args = _args_cobro("activate", tenant.tenant_id, "MP-9", "--plan", "control", "--months", "3")
    assert await mod.cmd_activate(db_session, args) == 1
    assert "un mes por pago" in capsys.readouterr().out
    await db_session.refresh(sub)
    assert sub.status == "TRIAL"


async def test_set_status_no_reinicia_una_prueba_ni_inventa_un_active(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    tenant, sub = trial
    sin_periodo = mod.parse_args(
        ["set-status", str(tenant.tenant_id), "--status", "ACTIVE", "--apply"]
    )
    assert await mod.cmd_set_status(db_session, sin_periodo) == 1

    await mod.cmd_activate(db_session, _args_activate(tenant.tenant_id, apply=True))
    a_trial = mod.parse_args(["set-status", str(tenant.tenant_id), "--status", "TRIAL", "--apply"])
    assert await mod.cmd_set_status(db_session, a_trial) == 1
    await db_session.refresh(sub)
    assert sub.status == "ACTIVE"


async def test_expire_due_no_manda_a_gracia_a_quien_pago_por_adelantado(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    _, sub = trial
    ahora = datetime.now(UTC)
    sub.status, sub.plan_code = "ACTIVE", "control"
    sub.current_period_start = ahora - timedelta(days=32)
    sub.current_period_end = ahora - timedelta(days=2)
    sub.next_period_end = ahora + timedelta(days=28)
    sub.next_granted_ia_queries_per_month = 70
    await db_session.commit()

    assert await mod.cmd_expire_due(db_session, mod.parse_args(["expire-due", "--apply"])) == 0

    await db_session.refresh(sub)
    assert sub.status == "ACTIVE"
    assert sub.next_period_end is None
    assert _sin_zona(sub.current_period_end) > _sin_zona(ahora)  # el pase quedó escrito


async def test_expire_due_cierra_una_cancelacion_cumplida_sin_pasar_por_gracia(
    mod: Any, db_session: AsyncSession, trial: tuple[Tenant, Subscription]
) -> None:
    _, sub = trial
    ahora = datetime.now(UTC)
    sub.status, sub.plan_code, sub.cancel_at_period_end = "ACTIVE", "control", True
    sub.current_period_start = ahora - timedelta(days=32)
    sub.current_period_end = ahora - timedelta(days=2)
    await db_session.commit()

    assert await mod.cmd_expire_due(db_session, mod.parse_args(["expire-due", "--apply"])) == 0

    await db_session.refresh(sub)
    assert sub.status == "CANCELLED"
