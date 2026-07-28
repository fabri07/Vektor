"""Integración PostgreSQL de la unificación de códigos de vertical (Task 5).

Por qué contra Postgres real
----------------------------
SQLite no valida los CHECK de tabla con la misma fidelidad que Postgres ni
soporta índices únicos PARCIALES sobre una expresión (``lower(email)``), que es
justo la forma que tienen las garantías que se prueban acá. El repo ya tiene el
antecedente documentado de un CHECK que pasó en SQLite y rompió en Postgres.

Qué cubre
---------
1. Las tres funciones de la migración ``20260806_0002_unify_vertical_codes.py``:
   la reescritura de los códigos legados en las TRES tablas, el fail-safe
   ``_verify_verticals_canonical`` (que aborta el deploy en Railway) y el CHECK
   final sobre ``business_profiles``.
2. Los CHECK de ``access_requests`` (Task 4), que hasta acá no tenían ninguna
   aserción formal contra el motor real: ``'otros'`` inescribible como vertical
   asignado, ``requested_plan`` fuera del catálogo, aprobada-sin-vertical, y el
   índice único parcial de un solo trámite abierto por email.
3. Que los CHECK DESPLEGADOS no divergieron de los enums del dominio. El modelo
   los deriva de ``Vertical``/``AccessRequestStatus``/``RequestedPlan``, pero la
   migración los escribió como literales (a propósito: una migración es una foto
   del pasado). Sin esta comparación, si un enum crece, ``create_all`` generaría
   en los tests un CHECK más permisivo que el de producción y SQLite no lo
   delataría.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_vertical_unification_pg.py -v --no-cov

Aislamiento bajo xdist: los tests de ``_migrate_vertical_codes`` /
``_verify_verticals_canonical`` usan TABLAS TEMPORALES homónimas (el ``pg_temp``
de la sesión precede a ``public`` en el ``search_path``, así que las queries sin
calificar de la migración leen la temporal) — no tocan las tablas reales ni
pisan a otro worker. Los tests de constraints sí usan las tablas reales, pero
con ``tenant_id`` / email únicos por test, que se limpian al final.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.domain.access_request import AccessRequestStatus, RequestedPlan
from app.domain.verticals import OPERATIONAL_VERTICALS
from app.persistence.db.base import Base
from app.persistence.models.access_request import AccessRequest, AccessRequestToken
from app.persistence.models.analytics_event import AnalyticsEvent
from app.persistence.models.business import BusinessProfile
from app.persistence.models.score import HeuristicRuleSet
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F86  # "VEKTOR" + 20260806_0002

_OPEN_EMAIL_INDEX = "uq_access_requests_open_email"
_CHECK_VERTICAL = "ck_business_profiles_vertical_code"


def _load_migration() -> Any:
    """Importa la migración por path: ``versions/`` no es un paquete importable."""
    ruta = (
        pathlib.Path(__file__).resolve().parents[2]
        / "persistence"
        / "migrations"
        / "versions"
        / "20260806_0002_unify_vertical_codes.py"
    )
    spec = importlib.util.spec_from_file_location("_unify_verticals_migration", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


mig = _load_migration()


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    tables: list[Table] = [
        cast("Table", Tenant.__table__),
        cast("Table", User.__table__),
        cast("Table", BusinessProfile.__table__),
        cast("Table", AnalyticsEvent.__table__),
        cast("Table", HeuristicRuleSet.__table__),
        cast("Table", AccessRequest.__table__),
        cast("Table", AccessRequestToken.__table__),
    ]
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        # checkfirst: en CI el schema ya lo creó `alembic upgrade head` (que es
        # quien PRUEBA las migraciones de verdad). Acá queda no-op.
        await conn.run_sync(Base.metadata.create_all, tables=tables, checkfirst=True)
        # Dos objetos que viven SOLO en las migraciones (no en los modelos), así
        # que `create_all` no los produce. Ambas llamadas son idempotentes: en CI,
        # donde alembic ya corrió, quedan en no-op.
        await conn.run_sync(lambda c: mig._add_vertical_check(c))
        await conn.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {_OPEN_EMAIL_INDEX} "
                "ON access_requests (lower(email)) "
                "WHERE status IN ('unverified', 'pending', 'waitlist')"
            )
        )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _crear_temp_business_profiles(conn: Any) -> None:
    """Sombra temporal de ``business_profiles`` con lo mínimo que toca la migración."""
    await conn.execute(
        text(
            "CREATE TEMP TABLE business_profiles ("
            "  profile_id uuid, vertical_code text, updated_at timestamptz)"
        )
    )


# ── `_migrate_vertical_codes` ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("legado", "canonico"),
    [
        ("kiosco", "kiosco_almacen"),
        ("almacen", "kiosco_almacen"),
        ("deco", "decoracion_hogar"),
        ("deco_hogar", "decoracion_hogar"),
        ("decoracion", "decoracion_hogar"),
        ("cleaning", "limpieza"),
    ],
)
async def test_migrate_reescribe_cada_alias_legado(
    pg_engine: AsyncEngine, legado: str, canonico: str
) -> None:
    async with pg_engine.connect() as conn:
        await _crear_temp_business_profiles(conn)
        pid = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO business_profiles VALUES "
                "(:id, :v, TIMESTAMPTZ '2020-01-01 00:00:00+00')"
            ),
            {"id": pid, "v": legado},
        )

        await conn.run_sync(lambda c: mig._migrate_vertical_codes(c))

        fila = (
            await conn.execute(
                text("SELECT vertical_code, updated_at FROM business_profiles WHERE profile_id=:i"),
                {"i": pid},
            )
        ).one()
        assert fila.vertical_code == canonico
        # El bump de `updated_at` es funcional: invalida el fingerprint del
        # estado cacheado en Redis (`business_state_service._make_fingerprint`).
        assert fila.updated_at.year > 2020


async def test_migrate_no_toca_codigos_canonicos_ni_desconocidos(pg_engine: AsyncEngine) -> None:
    """Un canónico queda igual; un código desconocido NO se adivina (no-invention)."""
    async with pg_engine.connect() as conn:
        await _crear_temp_business_profiles(conn)
        ok, raro = uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO business_profiles VALUES "
                "(:ok, 'limpieza', now()), (:raro, 'bogus', now())"
            ),
            {"ok": ok, "raro": raro},
        )

        await conn.run_sync(lambda c: mig._migrate_vertical_codes(c))

        filas = (
            await conn.execute(text("SELECT profile_id, vertical_code FROM business_profiles"))
        ).all()
        codigos: dict[uuid.UUID, str] = {fila[0]: fila[1] for fila in filas}
        assert codigos[ok] == "limpieza"
        assert codigos[raro] == "bogus"


async def test_migrate_es_idempotente(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as conn:
        await _crear_temp_business_profiles(conn)
        pid = uuid.uuid4()
        await conn.execute(
            text("INSERT INTO business_profiles VALUES (:i, 'kiosco', now())"), {"i": pid}
        )

        await conn.run_sync(lambda c: mig._migrate_vertical_codes(c))
        primera = (
            await conn.execute(
                text("SELECT updated_at FROM business_profiles WHERE profile_id=:i"), {"i": pid}
            )
        ).scalar_one()

        await conn.run_sync(lambda c: mig._migrate_vertical_codes(c))
        fila = (
            await conn.execute(
                text("SELECT vertical_code, updated_at FROM business_profiles WHERE profile_id=:i"),
                {"i": pid},
            )
        ).one()
        assert fila.vertical_code == "kiosco_almacen"
        # Segunda corrida: el WHERE ya no matchea, así que ni siquiera re-bumpea.
        assert fila.updated_at == primera


async def test_migrate_tambien_reescribe_analytics_events_y_rule_sets(
    pg_engine: AsyncEngine,
) -> None:
    """Sin esto, ``AnalyticsRepository.compute_margin_benchmark`` parte la muestra
    entre ``'kiosco'`` y ``'kiosco_almacen'`` y nunca llega a ``min_samples=5``."""
    async with pg_engine.connect() as conn:
        await _crear_temp_business_profiles(conn)
        await conn.execute(text("CREATE TEMP TABLE analytics_events (id uuid, vertical_code text)"))
        await conn.execute(text("CREATE TEMP TABLE heuristic_rule_sets (id uuid, vertical text)"))
        ev, rs = uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            text("INSERT INTO analytics_events VALUES (:i, 'kiosco')"), {"i": ev}
        )
        await conn.execute(
            text("INSERT INTO heuristic_rule_sets VALUES (:i, 'cleaning')"), {"i": rs}
        )

        await conn.run_sync(lambda c: mig._migrate_vertical_codes(c))

        assert (
            await conn.execute(
                text("SELECT vertical_code FROM analytics_events WHERE id=:i"), {"i": ev}
            )
        ).scalar_one() == "kiosco_almacen"
        assert (
            await conn.execute(
                text("SELECT vertical FROM heuristic_rule_sets WHERE id=:i"), {"i": rs}
            )
        ).scalar_one() == "limpieza"


# ── `_verify_verticals_canonical` (fail-safe de deploy) ──────────────────────


async def test_verify_pasa_con_solo_canonicos(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as conn:
        await _crear_temp_business_profiles(conn)
        await conn.execute(
            text(
                "INSERT INTO business_profiles VALUES "
                "(:a, 'kiosco_almacen', now()), (:b, 'limpieza', now()), "
                "(:c, 'decoracion_hogar', now())"
            ),
            {"a": uuid.uuid4(), "b": uuid.uuid4(), "c": uuid.uuid4()},
        )
        await conn.run_sync(lambda c: mig._verify_verticals_canonical(c))  # no levanta


async def test_migrate_luego_verify_reescribe_kiosco_y_aborta_por_bogus(
    pg_engine: AsyncEngine,
) -> None:
    """El escenario completo del deploy: lo migrable se migra, y lo que no está
    en el catálogo corta el deploy con los ofensores CONCRETOS en el mensaje
    (Railway aborta y la versión vieja sigue sirviendo)."""
    async with pg_engine.connect() as conn:
        await _crear_temp_business_profiles(conn)
        viejo, raro = uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO business_profiles VALUES "
                "(:v, 'kiosco', now()), (:r, 'bogus', now())"
            ),
            {"v": viejo, "r": raro},
        )

        await conn.run_sync(lambda c: mig._migrate_vertical_codes(c))
        assert (
            await conn.execute(
                text("SELECT vertical_code FROM business_profiles WHERE profile_id=:i"),
                {"i": viejo},
            )
        ).scalar_one() == "kiosco_almacen"

        with pytest.raises(RuntimeError, match="ABORTADA") as exc:
            await conn.run_sync(lambda c: mig._verify_verticals_canonical(c))
        assert "bogus" in str(exc.value)
        # Nombra la cantidad de filas afectadas, no solo el código.
        assert "1 perfil" in str(exc.value)


async def test_verify_lista_todos_los_ofensores(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as conn:
        await _crear_temp_business_profiles(conn)
        await conn.execute(
            text(
                "INSERT INTO business_profiles VALUES "
                "(:a, 'bogus', now()), (:b, 'ferreteria', now())"
            ),
            {"a": uuid.uuid4(), "b": uuid.uuid4()},
        )
        with pytest.raises(RuntimeError) as exc:
            await conn.run_sync(lambda c: mig._verify_verticals_canonical(c))
        mensaje = str(exc.value)
        assert "bogus" in mensaje
        assert "ferreteria" in mensaje


# ── El CHECK real sobre `business_profiles` ──────────────────────────────────


@pytest_asyncio.fixture
async def tenant_id(pg_engine: AsyncEngine) -> AsyncGenerator[uuid.UUID, None]:
    tid = uuid.uuid4()
    maker = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with maker() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T"))
        await s.commit()
    try:
        yield tid
    finally:
        async with maker() as s:
            await s.execute(delete(BusinessProfile).where(BusinessProfile.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


_INSERT_PROFILE = text(
    "INSERT INTO business_profiles "
    "(profile_id, tenant_id, vertical_code, data_mode, data_confidence, "
    " onboarding_completed, heuristic_profile_version, heuristics_version, "
    " created_at, updated_at) "
    "VALUES (:pid, :tid, :v, 'M0', 'LOW', false, 'v1', 'v1', now(), now())"
)


async def test_check_rechaza_el_codigo_corto_legado(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> None:
    """El CHECK es lo que impide que un import, un script o un SQL a mano vuelvan
    a meter ``'kiosco'`` después de la unificación."""
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as conn:
            await conn.execute(
                _INSERT_PROFILE, {"pid": uuid.uuid4(), "tid": tenant_id, "v": "kiosco"}
            )


async def test_check_acepta_los_canonicos(pg_engine: AsyncEngine, tenant_id: uuid.UUID) -> None:
    """Control positivo: sin esto el test anterior podría estar pasando por un
    typo de columna en vez de por el CHECK."""
    pid = uuid.uuid4()
    async with pg_engine.begin() as conn:
        await conn.execute(_INSERT_PROFILE, {"pid": pid, "tid": tenant_id, "v": "limpieza"})
    async with pg_engine.connect() as conn:
        total = (
            await conn.execute(
                text("SELECT count(*) FROM business_profiles WHERE profile_id=:i"), {"i": pid}
            )
        ).scalar_one()
    assert total == 1


async def test_add_vertical_check_es_idempotente(pg_engine: AsyncEngine) -> None:
    """La constraint ya está puesta (por alembic o por el fixture): una segunda
    llamada no debe explotar con ``duplicate object``."""
    async with pg_engine.begin() as conn:
        await conn.run_sync(lambda c: mig._add_vertical_check(c))


# ── Los CHECK de `access_requests` (huecos que dejó la Task 4) ────────────────


def _campos_solicitud(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "full_name": "Ana Pérez",
        "email": f"ana-{uuid.uuid4().hex[:10]}@example.com",
        "business_name": "Kiosco San Juan",
        "requested_vertical": "kiosco_almacen",
        "vertical_other_text": None,
        "requested_plan": "free",
        "years_operating": "2y_5y",
        "staff_size": "2_5",
        "monthly_revenue_band": "3m_10m",
        "main_concern": "CASH",
        "records_format": "planilla",
        "history_depth": "1y_3y",
        "can_share_files": "si_ordenados",
        "status": "unverified",
        "assigned_vertical_code": None,
        "consent_version": "v1",
    }
    base.update(overrides)
    return base


_INSERT_SOLICITUD = text(
    "INSERT INTO access_requests "
    "(id, full_name, email, business_name, requested_vertical, vertical_other_text, "
    " requested_plan, years_operating, staff_size, monthly_revenue_band, main_concern, "
    " records_format, history_depth, can_share_files, status, assigned_vertical_code, "
    " verification_email_status, owner_notification_status, decision_email_status, "
    " consent_version, consent_accepted_at, created_at, updated_at) "
    "VALUES (:id, :full_name, :email, :business_name, :requested_vertical, "
    " :vertical_other_text, :requested_plan, :years_operating, :staff_size, "
    " :monthly_revenue_band, :main_concern, :records_format, :history_depth, "
    " :can_share_files, :status, :assigned_vertical_code, 'pending', 'pending', "
    " 'pending', :consent_version, now(), now(), now())"
)


@pytest_asyncio.fixture
async def limpiar_solicitudes(pg_engine: AsyncEngine) -> AsyncGenerator[str, None]:
    """Devuelve un email único y borra al final lo que se haya insertado con él."""
    email = f"solicitud-{uuid.uuid4().hex[:12]}@example.com"
    try:
        yield email
    finally:
        async with pg_engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM access_requests WHERE lower(email) = lower(:e)"), {"e": email}
            )


async def test_access_request_check_rechaza_otros_como_vertical_asignado(
    pg_engine: AsyncEngine, limpiar_solicitudes: str
) -> None:
    """``'otros'`` es INESCRIBIBLE como vertical operativo: es la garantía de que
    un rubro no soportado no puede autodeterminarse por un bug de aplicación."""
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as conn:
            await conn.execute(
                _INSERT_SOLICITUD,
                _campos_solicitud(
                    email=limpiar_solicitudes,
                    status="approved",
                    assigned_vertical_code="otros",
                ),
            )


async def test_access_request_check_rechaza_plan_desconocido(
    pg_engine: AsyncEngine, limpiar_solicitudes: str
) -> None:
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as conn:
            await conn.execute(
                _INSERT_SOLICITUD,
                _campos_solicitud(email=limpiar_solicitudes, requested_plan="enterprise"),
            )


async def test_access_request_check_rechaza_aprobada_sin_vertical(
    pg_engine: AsyncEngine, limpiar_solicitudes: str
) -> None:
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as conn:
            await conn.execute(
                _INSERT_SOLICITUD,
                _campos_solicitud(
                    email=limpiar_solicitudes, status="approved", assigned_vertical_code=None
                ),
            )


async def test_access_request_check_rechaza_otros_sin_explicacion(
    pg_engine: AsyncEngine, limpiar_solicitudes: str
) -> None:
    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as conn:
            await conn.execute(
                _INSERT_SOLICITUD,
                _campos_solicitud(
                    email=limpiar_solicitudes,
                    requested_vertical="otros",
                    vertical_other_text=None,
                ),
            )


async def test_access_request_acepta_una_solicitud_valida(
    pg_engine: AsyncEngine, limpiar_solicitudes: str
) -> None:
    """Control positivo de los cuatro tests anteriores."""
    async with pg_engine.begin() as conn:
        await conn.execute(_INSERT_SOLICITUD, _campos_solicitud(email=limpiar_solicitudes))
    async with pg_engine.connect() as conn:
        total = (
            await conn.execute(
                text("SELECT count(*) FROM access_requests WHERE email=:e"),
                {"e": limpiar_solicitudes},
            )
        ).scalar_one()
    assert total == 1


async def test_indice_parcial_rechaza_segundo_tramite_abierto_con_otra_capitalizacion(
    pg_engine: AsyncEngine, limpiar_solicitudes: str
) -> None:
    """``uq_access_requests_open_email`` es sobre ``lower(email)``: dos trámites
    abiertos del mismo mail con distinta capitalización tienen que colisionar."""
    async with pg_engine.begin() as conn:
        await conn.execute(_INSERT_SOLICITUD, _campos_solicitud(email=limpiar_solicitudes))

    with pytest.raises(IntegrityError):
        async with pg_engine.begin() as conn:
            await conn.execute(
                _INSERT_SOLICITUD,
                _campos_solicitud(email=limpiar_solicitudes.upper(), status="pending"),
            )


async def test_indice_parcial_permite_volver_a_solicitar_tras_un_rechazo(
    pg_engine: AsyncEngine, limpiar_solicitudes: str
) -> None:
    """El índice es PARCIAL a propósito: ``rejected`` no bloquea un trámite nuevo
    (los negocios cambian). Si fuera un unique total, esto fallaría."""
    async with pg_engine.begin() as conn:
        await conn.execute(
            _INSERT_SOLICITUD,
            _campos_solicitud(email=limpiar_solicitudes, status="rejected"),
        )
        await conn.execute(_INSERT_SOLICITUD, _campos_solicitud(email=limpiar_solicitudes))

    async with pg_engine.connect() as conn:
        total = (
            await conn.execute(
                text("SELECT count(*) FROM access_requests WHERE lower(email)=lower(:e)"),
                {"e": limpiar_solicitudes},
            )
        ).scalar_one()
    assert total == 2


# ── Los CHECK desplegados vs. los enums del dominio (anti-divergencia) ───────


async def _definicion_check(conn: Any, nombre: str) -> str:
    definicion = (
        await conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = :n AND connamespace = 'public'::regnamespace"
            ),
            {"n": nombre},
        )
    ).scalar_one_or_none()
    assert definicion is not None, f"no existe la constraint {nombre} en public"
    return str(definicion)


def _literales(definicion: str) -> set[str]:
    """Extrae los literales entre comillas simples de un ``pg_get_constraintdef``.

    Postgres NO devuelve ``CHECK (status IN (...))``: para un ``IN`` sobre
    ``varchar`` normaliza a
    ``CHECK ((status)::text = ANY ((ARRAY['unverified'::character varying, ...])::text[]))``.
    Por eso se comparan los literales extraídos y no la cadena entera.
    """
    return set(re.findall(r"'([^']*)'", definicion))


@pytest.mark.parametrize(
    ("constraint", "esperados"),
    [
        ("ck_access_requests_status", {s.value for s in AccessRequestStatus}),
        ("ck_access_requests_requested_plan", {p.value for p in RequestedPlan}),
        ("ck_access_requests_assigned_vertical_code", set(OPERATIONAL_VERTICALS)),
        (_CHECK_VERTICAL, set(OPERATIONAL_VERTICALS)),
    ],
)
async def test_check_desplegado_no_divergio_del_enum(
    pg_engine: AsyncEngine, constraint: str, esperados: set[str]
) -> None:
    """Si un enum del dominio crece y la migración no lo acompaña, el CHECK
    desplegado queda más restrictivo que el código y esto lo delata. Es la única
    aserción que ata la foto del pasado (los literales de la migración) a la
    definición viva (los enums)."""
    async with pg_engine.connect() as conn:
        definicion = await _definicion_check(conn, constraint)
    assert _literales(definicion) == esperados, definicion
