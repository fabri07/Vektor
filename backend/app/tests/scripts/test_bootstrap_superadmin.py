"""Tests de ``scripts/bootstrap_superadmin.py`` — el único camino de recuperación.

Sobre una base recién reseteada este script es lo ÚNICO que puede crear un
``SUPERADMIN``: ``POST /auth/register`` responde 410 y aprobar una solicitud
acuña un OWNER, no un superadmin. Lo que se prueba acá es lo que hace que sea
seguro y suficiente:

1. **El dry-run no escribe.** Se corre el alta sin ``--apply`` y se consulta la
   base: no hay Tenant, ni User, ni token de invitación, ni auditoría.
2. **El alta deja ``role_code == "SUPERADMIN"``** — no OWNER, que es lo que
   crea ``provision_tenant``.
3. **``--promote`` sobre un email inexistente falla sin tocar nada.**
4. **La contraseña nunca aparece por CLI**: no hay flag para pasarla y lo único
   que se imprime es la URL de ``/definir-password``.

Se carga el módulo por ruta de archivo (``scripts/`` no es un paquete) — mismo
patrón que ``test_access_requests_script.py``.

Los handlers se invocan directo con la sesión del test: ``main()`` construye su
propio engine desde ``DATABASE_URL`` y no es testeable sin una base real. La
guardia del ``--apply`` que estos tests ejercitan vive en los handlers, no en
``main()`` — el ``rollback`` de ``main()`` es una red de seguridad adicional.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.auth_token import PasswordResetToken
from app.persistence.models.business import BusinessProfile, MomentumProfile
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module() -> Any:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "bootstrap_superadmin", _SCRIPTS_DIR / "bootstrap_superadmin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod() -> Any:
    return _load_module()


def _argv_alta(*extra: str) -> list[str]:
    return [
        "--email",
        "dueño@vektor.app",
        "--full-name",
        "Fabrizio Sola",
        "--business-name",
        "Véktor",
        "--vertical",
        "kiosco_almacen",
        *extra,
    ]


async def _contar(session: AsyncSession, modelo: Any) -> int:
    return int((await session.execute(select(func.count()).select_from(modelo))).scalar_one())


# ── El dry-run no escribe ────────────────────────────────────────────────────


async def test_dry_run_del_alta_no_escribe_nada(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    args = mod.parse_args(_argv_alta())
    assert args.apply is False
    assert await mod.cmd_alta(db_session, args) == 0

    # Se consulta la base, no se asume.
    assert await _contar(db_session, Tenant) == 0
    assert await _contar(db_session, User) == 0
    assert await _contar(db_session, Subscription) == 0
    assert await _contar(db_session, BusinessProfile) == 0
    assert await _contar(db_session, MomentumProfile) == 0
    assert await _contar(db_session, PasswordResetToken) == 0
    assert await _contar(db_session, DecisionAuditLog) == 0

    salida = capsys.readouterr().out
    assert "[DRY-RUN]" in salida
    assert "no se escribió nada" in salida
    # El dry-run tiene que decir el role_code final: es todo el punto del script.
    assert "SUPERADMIN" in salida


async def test_el_dry_run_anuncia_el_role_code_final_y_las_cinco_filas(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    await mod.cmd_alta(db_session, mod.parse_args(_argv_alta()))
    salida = capsys.readouterr().out
    assert "role_code final:" in salida
    assert "SUPERADMIN" in salida
    assert "Tenant a crear:" in salida
    assert "Usuario a crear:" in salida
    assert "FREE" in salida
    assert "kiosco_almacen" in salida


async def test_dry_run_de_promote_no_asciende(
    mod: Any, db_session: AsyncSession, sample_tenant: Tenant, capsys: Any
) -> None:
    user = User(
        tenant_id=sample_tenant.tenant_id,
        email="dueño@vektor.app",
        full_name="Fabrizio Sola",
        password_hash="x",
        role_code="OWNER",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    args = mod.parse_args(["--promote", "dueño@vektor.app"])
    assert args.apply is False
    assert await mod.cmd_promote(db_session, args) == 0

    vigente = (
        await db_session.execute(select(User.role_code).where(User.user_id == user.user_id))
    ).scalar_one()
    assert vigente == "OWNER"
    assert await _contar(db_session, DecisionAuditLog) == 0
    assert "no se escribió nada" in capsys.readouterr().out


# ── El alta deja SUPERADMIN ──────────────────────────────────────────────────


async def test_el_alta_con_apply_deja_role_code_superadmin(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    assert await mod.cmd_alta(db_session, mod.parse_args(_argv_alta("--apply"))) == 0

    user = (await db_session.execute(select(User))).scalar_one()
    # provision_tenant crea OWNER; si el script se olvidara del ascenso, acá
    # diría "OWNER" y el dueño quedaría igual de afuera de /admin/*.
    assert user.role_code == "SUPERADMIN"
    assert user.email == "dueño@vektor.app"
    assert user.is_active is True

    # Las 5 filas del acuñado salen de provision_tenant, no de este script.
    assert await _contar(db_session, Tenant) == 1
    assert await _contar(db_session, Subscription) == 1
    assert await _contar(db_session, BusinessProfile) == 1
    assert await _contar(db_session, MomentumProfile) == 1

    perfil = (await db_session.execute(select(BusinessProfile))).scalar_one()
    assert perfil.vertical_code == "kiosco_almacen"
    suscripcion = (await db_session.execute(select(Subscription))).scalar_one()
    assert suscripcion.plan_code == "FREE"


async def test_el_alta_emite_el_link_de_invitacion_y_nunca_una_contrasena(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    assert await mod.cmd_alta(db_session, mod.parse_args(_argv_alta("--apply"))) == 0

    token = (await db_session.execute(select(PasswordResetToken))).scalar_one()
    assert token.used is False

    salida = capsys.readouterr().out
    assert f"definir-password?token={token.token_id}" in salida
    # El hash aleatorio que puso provision_tenant no se filtra por la salida.
    user = (await db_session.execute(select(User))).scalar_one()
    assert user.password_hash not in salida


def test_la_cli_no_tiene_forma_de_recibir_una_contrasena(mod: Any) -> None:
    """Una contraseña por CLI quedaría en el historial del shell y en ``ps``."""
    for flag in ("--password", "--pass", "--password-hash"):
        with pytest.raises(SystemExit):
            mod.parse_args(_argv_alta(flag, "loquesea"))


async def test_el_alta_audita_la_decision(mod: Any, db_session: AsyncSession) -> None:
    assert await mod.cmd_alta(db_session, mod.parse_args(_argv_alta("--apply"))) == 0

    traza = (await db_session.execute(select(DecisionAuditLog))).scalar_one()
    assert traza.decision_type == "SUPERADMIN_BOOTSTRAP"
    assert traza.triggered_by == "script:bootstrap_superadmin"
    assert traza.decision_data["role_code"] == "SUPERADMIN"


# ── Idempotencia ─────────────────────────────────────────────────────────────


async def test_el_alta_sobre_un_email_existente_falla_y_sugiere_promote(
    mod: Any, db_session: AsyncSession, sample_tenant: Tenant, capsys: Any
) -> None:
    db_session.add(
        User(
            tenant_id=sample_tenant.tenant_id,
            email="dueño@vektor.app",
            full_name="Fabrizio Sola",
            password_hash="x",
            role_code="OWNER",
            is_active=True,
        )
    )
    await db_session.flush()
    tenants_antes = await _contar(db_session, Tenant)

    assert await mod.cmd_alta(db_session, mod.parse_args(_argv_alta("--apply"))) == 1

    assert await _contar(db_session, Tenant) == tenants_antes
    assert await _contar(db_session, User) == 1
    salida = capsys.readouterr().out
    assert "YA tiene cuenta" in salida
    assert "--promote" in salida


async def test_promote_de_un_email_inexistente_falla_sin_tocar_nada(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    args = mod.parse_args(["--promote", "fantasma@vektor.app", "--apply"])
    assert await mod.cmd_promote(db_session, args) == 1

    assert await _contar(db_session, User) == 0
    assert await _contar(db_session, Tenant) == 0
    assert await _contar(db_session, DecisionAuditLog) == 0
    assert "no existe ningún usuario" in capsys.readouterr().out


async def test_promote_con_apply_asciende_y_audita(
    mod: Any, db_session: AsyncSession, sample_tenant: Tenant, capsys: Any
) -> None:
    user = User(
        tenant_id=sample_tenant.tenant_id,
        email="dueño@vektor.app",
        full_name="Fabrizio Sola",
        password_hash="hash-original",
        role_code="OWNER",
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    args = mod.parse_args(["--promote", "DUEÑO@VEKTOR.APP", "--apply"])
    assert await mod.cmd_promote(db_session, args) == 0

    await db_session.refresh(user)
    assert user.role_code == "SUPERADMIN"
    # La promoción no toca la contraseña ni emite tokens.
    assert user.password_hash == "hash-original"
    assert await _contar(db_session, PasswordResetToken) == 0

    traza = (await db_session.execute(select(DecisionAuditLog))).scalar_one()
    assert traza.decision_type == "SUPERADMIN_PROMOTED"
    assert traza.decision_data["role_code_previo"] == "OWNER"
    assert "COMMIT" in capsys.readouterr().out


async def test_promote_de_un_superadmin_no_reescribe(
    mod: Any, db_session: AsyncSession, sample_tenant: Tenant, capsys: Any
) -> None:
    db_session.add(
        User(
            tenant_id=sample_tenant.tenant_id,
            email="dueño@vektor.app",
            full_name="Fabrizio Sola",
            password_hash="x",
            role_code="SUPERADMIN",
            is_active=True,
        )
    )
    await db_session.flush()

    args = mod.parse_args(["--promote", "dueño@vektor.app", "--apply"])
    assert await mod.cmd_promote(db_session, args) == 0
    assert await _contar(db_session, DecisionAuditLog) == 0
    assert "YA es SUPERADMIN" in capsys.readouterr().out


async def test_promote_de_una_cuenta_inactiva_falla_sin_ascender(
    mod: Any, db_session: AsyncSession, sample_tenant: Tenant, capsys: Any
) -> None:
    """Un SUPERADMIN inactivo no puede loguear: ascenderlo no saca a nadie del bloqueo.

    ``AuthService.login`` corta por ``is_active`` ANTES de mirar el ``role_code``.
    Sin esta guarda el script imprimiría "COMMIT: pasó de OWNER a SUPERADMIN" y
    el dueño seguiría afuera, creyendo que la recuperación salió bien.
    """
    user = User(
        tenant_id=sample_tenant.tenant_id,
        email="dueño@vektor.app",
        full_name="Fabrizio Sola",
        password_hash="x",
        role_code="OWNER",
        is_active=False,
    )
    db_session.add(user)
    await db_session.flush()

    args = mod.parse_args(["--promote", "dueño@vektor.app", "--apply"])
    assert await mod.cmd_promote(db_session, args) == 1

    await db_session.refresh(user)
    assert user.role_code == "OWNER"
    # Tampoco se activa por su cuenta: is_active=False es email sin verificar, y
    # saltearlo desde una consola sería el atajo que el sistema nuevo cerró.
    assert user.is_active is False
    assert await _contar(db_session, DecisionAuditLog) == 0
    assert "INACTIVA" in capsys.readouterr().out


async def test_promote_con_email_ambiguo_falla_sin_elegir(
    mod: Any, db_session: AsyncSession, sample_tenant: Tenant, capsys: Any
) -> None:
    """``users.email`` no es único global: ascender "el primero" sería a ciegas."""
    otro = Tenant(
        legal_name="Otro SRL",
        display_name="Otro SRL",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(otro)
    await db_session.flush()
    for tenant_id in (sample_tenant.tenant_id, otro.tenant_id):
        db_session.add(
            User(
                tenant_id=tenant_id,
                email="dueño@vektor.app",
                full_name="Fabrizio Sola",
                password_hash="x",
                role_code="OWNER",
                is_active=True,
            )
        )
    await db_session.flush()

    args = mod.parse_args(["--promote", "dueño@vektor.app", "--apply"])
    assert await mod.cmd_promote(db_session, args) == 1

    roles = (await db_session.execute(select(User.role_code))).scalars().all()
    assert set(roles) == {"OWNER"}
    assert "no elige por vos" in capsys.readouterr().out


# ── La CLI ───────────────────────────────────────────────────────────────────


def test_apply_es_false_por_defecto_en_los_dos_modos(mod: Any) -> None:
    assert mod.parse_args(_argv_alta()).apply is False
    assert mod.parse_args(["--promote", "x@y.test"]).apply is False


def test_el_alta_exige_los_cuatro_campos(mod: Any) -> None:
    with pytest.raises(SystemExit):
        mod.parse_args(["--email", "x@y.test", "--full-name", "X"])
    with pytest.raises(SystemExit):
        mod.parse_args([])


def test_promote_es_excluyente_con_el_alta(mod: Any) -> None:
    with pytest.raises(SystemExit):
        mod.parse_args(["--promote", "x@y.test", "--email", "x@y.test"])


def test_solo_los_tres_verticales_operativos_son_decibles(mod: Any) -> None:
    for codigo in ("kiosco_almacen", "decoracion_hogar", "limpieza"):
        args = mod.parse_args(
            [
                "--email",
                "x@y.test",
                "--full-name",
                "X",
                "--business-name",
                "N",
                "--vertical",
                codigo,
            ]
        )
        assert args.vertical == codigo
    with pytest.raises(SystemExit):
        mod.parse_args(
            [
                "--email",
                "x@y.test",
                "--full-name",
                "X",
                "--business-name",
                "N",
                "--vertical",
                "otros",
            ]
        )


def test_el_handler_queda_cableado_segun_el_modo(mod: Any) -> None:
    assert mod.parse_args(_argv_alta()).handler is mod.cmd_alta
    assert mod.parse_args(["--promote", "x@y.test"]).handler is mod.cmd_promote
