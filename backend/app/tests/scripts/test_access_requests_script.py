"""Tests de ``scripts/access_requests.py`` — la consola de solicitudes del dueño.

Lo que se prueba acá es lo que hace que el script sea seguro de operar, no su
formato:

1. **El dry-run no escribe.** Cada comando mutante se corre SIN ``--apply`` y se
   verifica contra la base que no cambió nada — no se asume, se consulta. Si se
   borrara la guardia del ``--apply``, estos tests fallan (``approve`` acuñaría
   el tenant, ``expire-stale`` transicionaría las filas).
2. **``--plan premium`` filtra de verdad.**
3. **``otros`` es indecible en ``--vertical``**: argparse lo rechaza.
4. **La aprobación pasa por el servicio** y crea una suscripción ``FREE`` aunque
   la intención declarada haya sido ``premium``.
5. **Nunca se imprime el ``ip_hash``.**
6. **El script previene, el servicio decide.** Los avisos del dry-run son
   diagnóstico: con ``--apply`` se delega igual y el código 1 sale de la
   excepción del servicio, para que la CLI no tenga una copia propia —y algún
   día desactualizada— de qué está permitido.

Se carga el módulo por ruta de archivo (``scripts/`` no es un paquete) — mismo
patrón que ``test_detect_misvoided_purchases.py``. Importar el módulo dispara
``from _db import async_engine_config`` a nivel de módulo, pero ``_db`` solo
define helpers (no conecta), así que el import es seguro sin ``DATABASE_URL``.

Los handlers se invocan directo con la sesión del test: ``main()`` construye su
propio engine desde ``DATABASE_URL`` y no es testeable sin una base real. La
guardia del ``--apply`` que estos tests ejercitan vive en los handlers, no en
``main()`` — el ``rollback`` de ``main()`` es una red de seguridad adicional.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.access_request import AccessRequestStatus, RequestedPlan
from app.persistence.models.access_request import AccessRequest
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module() -> Any:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "access_requests", _SCRIPTS_DIR / "access_requests.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod() -> Any:
    return _load_module()


# ── Sembrado ─────────────────────────────────────────────────────────────────


def _solicitud(
    *,
    email: str,
    business_name: str,
    plan: RequestedPlan = RequestedPlan.FREE,
    status: AccessRequestStatus = AccessRequestStatus.PENDING,
    vertical: str = "kiosco_almacen",
    vertical_other_text: str | None = None,
    verificada: bool = True,
    creada_hace_dias: int = 0,
    ip_hash: str | None = None,
) -> AccessRequest:
    ahora = datetime.now(UTC)
    creada = ahora - timedelta(days=creada_hace_dias)
    # El CHECK `ck_access_requests_approved_needs_vertical` no deja aprobar sin
    # vertical asignado: una solicitud sembrada como `approved` tiene que
    # traerlo, igual que la que acuña el servicio.
    asignado = vertical if status is AccessRequestStatus.APPROVED else None
    return AccessRequest(
        full_name="Juan Pérez",
        email=email,
        phone=None,
        business_name=business_name,
        requested_vertical=vertical,
        vertical_other_text=vertical_other_text,
        requested_plan=plan.value,
        years_operating="2y_5y",
        staff_size="2_5",
        monthly_revenue_band="3m_10m",
        main_concern="MARGIN",
        records_format="planilla",
        history_depth="1y_3y",
        can_share_files="si_ordenados",
        status=status.value,
        assigned_vertical_code=asignado,
        email_verified_at=creada if verificada else None,
        verification_email_status="sent",
        owner_notification_status="sent",
        decision_email_status="pending",
        consent_version="v1",
        consent_accepted_at=creada,
        ip_hash=ip_hash,
        created_at=creada,
        updated_at=creada,
    )


@pytest_asyncio.fixture
async def premium(db_session: AsyncSession) -> AsyncGenerator[AccessRequest, None]:
    solicitud = _solicitud(
        email="premium@kiosco.test",
        business_name="Kiosco San Juan",
        plan=RequestedPlan.PREMIUM,
        ip_hash="a" * 64,
    )
    db_session.add(solicitud)
    await db_session.flush()
    yield solicitud


@pytest_asyncio.fixture
async def gratuita(db_session: AsyncSession) -> AsyncGenerator[AccessRequest, None]:
    solicitud = _solicitud(
        email="free@limpieza.test",
        business_name="Limpiezas del Sur",
        plan=RequestedPlan.FREE,
        vertical="limpieza",
    )
    db_session.add(solicitud)
    await db_session.flush()
    yield solicitud


async def _contar(session: AsyncSession, modelo: Any) -> int:
    return int(
        (
            await session.execute(select(func.count()).select_from(modelo))
        ).scalar_one()
    )


async def _estado(session: AsyncSession, request_id: uuid.UUID) -> str:
    return (
        await session.execute(
            select(AccessRequest.status).where(AccessRequest.id == request_id)
        )
    ).scalar_one()


# ── El dry-run no escribe ────────────────────────────────────────────────────


async def test_dry_run_de_approve_no_acuna_nada(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    tenants_antes = await _contar(db_session, Tenant)
    auditoria_antes = await _contar(db_session, DecisionAuditLog)

    args = mod.parse_args(
        ["approve", str(premium.id), "--vertical", "kiosco_almacen"]
    )
    assert args.apply is False
    assert await mod.cmd_approve(db_session, args) == 0

    # Se consulta la base, no se asume: nada cambió.
    assert await _estado(db_session, premium.id) == AccessRequestStatus.PENDING.value
    assert await _contar(db_session, Tenant) == tenants_antes
    assert await _contar(db_session, User) == 0
    assert await _contar(db_session, DecisionAuditLog) == auditoria_antes

    salida = capsys.readouterr().out
    assert "[DRY-RUN]" in salida
    assert "no se escribió nada" in salida


async def test_dry_run_de_reject_no_transiciona(
    mod: Any, db_session: AsyncSession, premium: AccessRequest
) -> None:
    args = mod.parse_args(
        ["reject", str(premium.id), "--reason", "fuera de los rubros soportados"]
    )
    assert await mod.cmd_reject(db_session, args) == 0
    assert await _estado(db_session, premium.id) == AccessRequestStatus.PENDING.value


async def test_dry_run_de_reject_avisa_que_el_motivo_es_muy_corto(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    """El dry-run no promete un rechazo que el ``--apply`` va a rebotar."""
    args = mod.parse_args(["reject", str(premium.id), "--reason", "ab"])
    assert await mod.cmd_reject(db_session, args) == 0
    assert "mínimo 3 caracteres" in capsys.readouterr().out
    assert await _estado(db_session, premium.id) == AccessRequestStatus.PENDING.value


async def test_dry_run_de_waitlist_no_transiciona(
    mod: Any, db_session: AsyncSession, premium: AccessRequest
) -> None:
    args = mod.parse_args(["waitlist", str(premium.id)])
    assert await mod.cmd_waitlist(db_session, args) == 0
    assert await _estado(db_session, premium.id) == AccessRequestStatus.PENDING.value


async def test_dry_run_de_expire_stale_lista_pero_no_expira(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    rancia = _solicitud(
        email="rancia@x.test",
        business_name="Nunca Confirmó SRL",
        status=AccessRequestStatus.UNVERIFIED,
        verificada=False,
        creada_hace_dias=40,
    )
    db_session.add(rancia)
    await db_session.flush()

    args = mod.parse_args(["expire-stale", "--days", "14"])
    assert await mod.cmd_expire_stale(db_session, args) == 0

    assert await _estado(db_session, rancia.id) == AccessRequestStatus.UNVERIFIED.value
    salida = capsys.readouterr().out
    # El dry-run de un comando en lote lista el detalle, no solo el conteo: con
    # --days mal puesto, "expiraría 47" sin saber cuáles es peligroso.
    assert "Solicitudes que se tocarían: 1" in salida
    assert str(rancia.id) in salida
    assert "Nunca Confirmó SRL" in salida


async def test_expire_stale_respeta_el_corte_de_dias(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    """Una ``unverified`` reciente no entra, y una ``pending`` vieja tampoco."""
    db_session.add(
        _solicitud(
            email="reciente@x.test",
            business_name="Recién Llegada",
            status=AccessRequestStatus.UNVERIFIED,
            verificada=False,
            creada_hace_dias=2,
        )
    )
    db_session.add(
        _solicitud(
            email="vieja.pending@x.test",
            business_name="Espera Hace Meses",
            status=AccessRequestStatus.PENDING,
            creada_hace_dias=90,
        )
    )
    await db_session.flush()

    args = mod.parse_args(["expire-stale", "--days", "14"])
    assert await mod.cmd_expire_stale(db_session, args) == 0
    salida = capsys.readouterr().out
    assert "Solicitudes que se tocarían: 0" in salida
    assert "Nada que expirar" in salida


async def test_expire_stale_con_apply_expira(
    mod: Any, db_session: AsyncSession
) -> None:
    rancia = _solicitud(
        email="rancia2@x.test",
        business_name="Nunca Confirmó SRL",
        status=AccessRequestStatus.UNVERIFIED,
        verificada=False,
        creada_hace_dias=40,
    )
    db_session.add(rancia)
    await db_session.flush()

    args = mod.parse_args(["expire-stale", "--days", "14", "--apply"])
    assert await mod.cmd_expire_stale(db_session, args) == 0
    assert await _estado(db_session, rancia.id) == AccessRequestStatus.EXPIRED.value


# ── approve --apply pasa por el servicio ─────────────────────────────────────


async def test_approve_con_apply_acuna_la_cuenta_con_suscripcion_free(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    """La intención Premium NO se convierte en una suscripción Premium."""
    args = mod.parse_args(
        ["approve", str(premium.id), "--vertical", "kiosco_almacen", "--apply"]
    )
    assert await mod.cmd_approve(db_session, args) == 0

    assert await _estado(db_session, premium.id) == AccessRequestStatus.APPROVED.value
    suscripciones = (
        (await db_session.execute(select(Subscription))).scalars().all()
    )
    assert len(suscripciones) == 1
    assert suscripciones[0].plan_code == "FREE"

    solicitud = (
        await db_session.execute(
            select(AccessRequest).where(AccessRequest.id == premium.id)
        )
    ).scalar_one()
    # Decidido desde el script, sin usuario logueado: así queda en la traza.
    assert solicitud.reviewed_via == "script"
    assert solicitud.reviewed_by_user_id is None
    assert solicitud.assigned_vertical_code == "kiosco_almacen"

    salida = capsys.readouterr().out
    assert "COMMIT: cuenta acuñada." in salida


async def test_approve_de_una_no_verificada_falla_y_no_acuna(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    """La guardia del doble opt-in la aplica el servicio, también desde el script."""
    sin_verificar = _solicitud(
        email="sinverificar@x.test",
        business_name="Sin Doble Opt-in",
        status=AccessRequestStatus.WAITLIST,
        verificada=False,
    )
    db_session.add(sin_verificar)
    await db_session.flush()

    args = mod.parse_args(
        ["approve", str(sin_verificar.id), "--vertical", "limpieza", "--apply"]
    )
    assert await mod.cmd_approve(db_session, args) == 1
    assert await _contar(db_session, Tenant) == 0
    assert "doble opt-in" in capsys.readouterr().out


# ── Las dos líneas separadas: plan solicitado ≠ suscripción ──────────────────


async def test_el_dry_run_separa_plan_solicitado_de_suscripcion(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    args = mod.parse_args(
        ["approve", str(premium.id), "--vertical", "kiosco_almacen"]
    )
    await mod.cmd_approve(db_session, args)
    salida = capsys.readouterr().out
    assert "Plan solicitado:" in salida
    assert "PREMIUM" in salida
    assert "Suscripción a crear:" in salida
    assert "FREE" in salida


async def test_show_separa_plan_solicitado_de_suscripcion(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    args = mod.parse_args(["show", str(premium.id)])
    assert await mod.cmd_show(db_session, args) == 0
    salida = capsys.readouterr().out
    assert "Plan solicitado:      PREMIUM" in salida
    assert "Suscripción a crear:  FREE" in salida


async def test_show_nunca_imprime_el_ip_hash(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    assert premium.ip_hash  # el fixture lo sembró
    args = mod.parse_args(["show", str(premium.id)])
    await mod.cmd_show(db_session, args)
    assert premium.ip_hash not in capsys.readouterr().out


async def test_show_acepta_email_ademas_del_id(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    args = mod.parse_args(["show", "PREMIUM@KIOSCO.TEST"])
    assert await mod.cmd_show(db_session, args) == 0
    assert "Kiosco San Juan" in capsys.readouterr().out


# ── list ─────────────────────────────────────────────────────────────────────


async def test_list_con_plan_premium_filtra(
    mod: Any,
    db_session: AsyncSession,
    premium: AccessRequest,
    gratuita: AccessRequest,
    capsys: Any,
) -> None:
    args = mod.parse_args(["list", "--plan", "premium"])
    assert await mod.cmd_list(db_session, args) == 0
    salida = capsys.readouterr().out
    assert "Kiosco San Juan" in salida
    assert "Limpiezas del Sur" not in salida
    assert "mostrando 1 de 1 solicitud/es." in salida


async def test_list_sin_filtro_muestra_las_dos(
    mod: Any,
    db_session: AsyncSession,
    premium: AccessRequest,
    gratuita: AccessRequest,
    capsys: Any,
) -> None:
    args = mod.parse_args(["list"])
    assert await mod.cmd_list(db_session, args) == 0
    salida = capsys.readouterr().out
    assert "Kiosco San Juan" in salida
    assert "Limpiezas del Sur" in salida
    # Premium primero: es el orden de prioridad de la cola.
    assert salida.index("Kiosco San Juan") < salida.index("Limpiezas del Sur")


async def test_list_no_muestra_las_unverified_en_la_cola(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    """El doble opt-in es el filtro que evita gastar revisión en emails falsos."""
    db_session.add(
        _solicitud(
            email="fantasma@x.test",
            business_name="Email Falso SA",
            plan=RequestedPlan.PREMIUM,
            status=AccessRequestStatus.UNVERIFIED,
            verificada=False,
        )
    )
    await db_session.flush()

    assert await mod.cmd_list(db_session, mod.parse_args(["list"])) == 0
    assert "Email Falso SA" not in capsys.readouterr().out


async def test_list_email_failed_solo_trae_las_que_fallaron(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    fallada = _solicitud(
        email="fallada@x.test",
        business_name="Nunca Recibió Nada",
        status=AccessRequestStatus.APPROVED,
    )
    fallada.decision_email_status = "failed"
    db_session.add(fallada)
    await db_session.flush()

    args = mod.parse_args(["list", "--email-failed"])
    assert await mod.cmd_list(db_session, args) == 0
    salida = capsys.readouterr().out
    assert "Nunca Recibió Nada" in salida
    assert "Kiosco San Juan" not in salida
    assert "MAIL FALLIDO: decisión" in salida


async def test_list_email_failed_anuncia_el_filtro_de_estado_en_el_titulo(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    """Sin eso, un resultado corto se lee como "no hay fallidos" y hay otro filtro."""
    args = mod.parse_args(["list", "--email-failed", "--status", "pending"])
    assert await mod.cmd_list(db_session, args) == 0
    assert "mail FALLIDO · estado: pending" in capsys.readouterr().out


async def test_list_status_all_incluye_estados_terminales(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    db_session.add(
        _solicitud(
            email="rechazada@x.test",
            business_name="Rechazada SRL",
            status=AccessRequestStatus.REJECTED,
        )
    )
    await db_session.flush()

    args = mod.parse_args(["list", "--status", "all"])
    assert await mod.cmd_list(db_session, args) == 0
    salida = capsys.readouterr().out
    assert "Rechazada SRL" in salida
    assert "Kiosco San Juan" in salida


# ── otros ────────────────────────────────────────────────────────────────────


async def test_otros_agrupa_por_descripcion_normalizada(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    for nombre, texto in (
        ("Repuestos Central", "Repuestos de moto"),
        ("Moto Norte", "  repuestos de MOTO  "),
        ("Pan del Barrio", "panadería"),
    ):
        db_session.add(
            _solicitud(
                email=f"{nombre.split()[0].lower()}@x.test",
                business_name=nombre,
                vertical="otros",
                vertical_other_text=texto,
            )
        )
    await db_session.flush()

    assert await mod.cmd_otros(db_session, mod.parse_args(["otros"])) == 0
    salida = capsys.readouterr().out
    assert "2  repuestos de moto" in salida
    assert "1  panadería" in salida
    assert "3 solicitud/es de rubro 'otros' en 2 descripción/es" in salida


# ── resend-invite ────────────────────────────────────────────────────────────


async def test_resend_invite_rechaza_los_estados_sin_mail_pendiente(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    """Una ``pending`` espera al DUEÑO, no al solicitante: no hay qué reenviar.

    Y el que lo rechaza es el SERVICIO, no el script: el aviso del preview no
    corta: con ``--apply`` se delega igual y el código 1 sale de
    ``AccessRequestNotResendable``. Si el script volviera a cortar por su cuenta,
    su lista de estados reencolables divergiría de la del servicio en silencio.
    """
    args = mod.parse_args(["resend-invite", str(premium.id), "--apply"])
    assert await mod.cmd_resend_invite(db_session, args) == 1
    salida = capsys.readouterr().out
    assert "no hay ningún mail" in salida  # el preview avisó
    # ...y la decisión la tomó el servicio, no el script.
    assert "ERROR: no se reenvió." in salida
    assert "no tiene mail para reencolar" in salida


async def test_dry_run_de_resend_invite_de_un_estado_sin_mail_no_escribe(
    mod: Any, db_session: AsyncSession, premium: AccessRequest, capsys: Any
) -> None:
    """El preview de un estado no reencolable avisa, pero sigue siendo un dry-run."""
    args = mod.parse_args(["resend-invite", str(premium.id)])
    assert await mod.cmd_resend_invite(db_session, args) == 0
    assert await _estado(db_session, premium.id) == AccessRequestStatus.PENDING.value
    salida = capsys.readouterr().out
    assert "no hay ningún mail" in salida
    assert "no se escribió nada" in salida


async def test_resend_invite_de_una_aprobada_sin_usuario_avisa_y_delega(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    """FK ``ON DELETE SET NULL``: el dry-run no promete un mail que el apply rebota."""
    huerfana = _solicitud(
        email="sinusuario@x.test",
        business_name="Cuenta Borrada SA",
        status=AccessRequestStatus.APPROVED,
    )
    db_session.add(huerfana)
    await db_session.flush()
    assert huerfana.approved_user_id is None

    assert (
        await mod.cmd_resend_invite(
            db_session, mod.parse_args(["resend-invite", str(huerfana.id)])
        )
        == 0
    )
    assert "ya no apunta a ningún usuario" in capsys.readouterr().out

    # Con --apply la autoridad es el servicio, que corta en vez de inventar a
    # quién invitar.
    assert (
        await mod.cmd_resend_invite(
            db_session, mod.parse_args(["resend-invite", str(huerfana.id), "--apply"])
        )
        == 1
    )
    assert "ERROR: no se reenvió." in capsys.readouterr().out


async def test_dry_run_de_resend_invite_no_emite_token(
    mod: Any, db_session: AsyncSession, capsys: Any
) -> None:
    from app.persistence.models.access_request import AccessRequestToken

    sin_verificar = _solicitud(
        email="sinverificar2@x.test",
        business_name="Sin Confirmar SA",
        status=AccessRequestStatus.UNVERIFIED,
        verificada=False,
    )
    db_session.add(sin_verificar)
    await db_session.flush()

    args = mod.parse_args(["resend-invite", str(sin_verificar.id)])
    assert await mod.cmd_resend_invite(db_session, args) == 0
    assert await _contar(db_session, AccessRequestToken) == 0
    assert "no se escribió nada" in capsys.readouterr().out


# ── La CLI ───────────────────────────────────────────────────────────────────


def test_otros_es_indecible_como_vertical_en_la_cli(mod: Any) -> None:
    """'otros' es una opción del formulario público, nunca un vertical operativo."""
    with pytest.raises(SystemExit):
        mod.parse_args(
            ["approve", str(uuid.uuid4()), "--vertical", "otros", "--apply"]
        )


def test_los_tres_verticales_reales_si_son_decibles(mod: Any) -> None:
    for codigo in ("kiosco_almacen", "decoracion_hogar", "limpieza"):
        args = mod.parse_args(
            ["approve", str(uuid.uuid4()), "--vertical", codigo]
        )
        assert args.vertical == codigo


def test_apply_es_false_por_defecto_en_todo_comando_mutante(mod: Any) -> None:
    identificador = str(uuid.uuid4())
    invocaciones = (
        ["approve", identificador, "--vertical", "limpieza"],
        ["reject", identificador, "--reason", "spam"],
        ["waitlist", identificador],
        ["resend-invite", identificador],
        ["expire-stale"],
    )
    for argv in invocaciones:
        assert mod.parse_args(argv).apply is False, argv


def test_expire_stale_usa_14_dias_por_defecto(mod: Any) -> None:
    assert mod.parse_args(["expire-stale"]).days == 14


def test_recortar_no_supera_el_ancho_pedido(mod: Any) -> None:
    assert mod.recortar("abcdefghij", 5) == "abcd…"
    assert len(mod.recortar("abcdefghij", 5)) == 5
    assert mod.recortar("abc", 10) == "abc"


def test_opcional_no_inventa_contenido(mod: Any) -> None:
    assert mod.opcional(None) == "—"
    assert mod.opcional("   ") == "—"
    assert mod.opcional(" hola ") == "hola"


def test_con_glosa_deja_pasar_un_codigo_desconocido(mod: Any) -> None:
    """No se inventa una etiqueta para una banda que el catálogo no conoce."""
    assert mod.con_glosa("banda_futura_99") == "banda_futura_99"
    assert mod.con_glosa("2y_5y") == "2y_5y (entre 2 y 5 años)"
