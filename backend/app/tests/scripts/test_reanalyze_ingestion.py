"""Tests de ``scripts/reanalyze_ingestion.py`` (Task 4 — comando de reanálisis).

``scripts/`` no es un paquete: el módulo se carga por ruta de archivo, mismo
patrón que ``test_reconcile_untagged_adjustments.py``/
``test_detect_misvoided_purchases.py``. A diferencia de esos precedentes (que
solo testean funciones puras sin DB), acá SÍ ejercitamos el flujo completo
contra el fixture ``db_session`` (SQLite in-memory) — reusa el mismo patrón de
fixtures que ``app/tests/services/test_reread_file.py`` (``_patch_s3``,
``_make_file``, ``_first_confirm_with_risk`` con una decisión de riesgo F8b+
persistida) para poner un archivo en cada outcome de ``column_risk_outcome``.

Cubre (ver brief Task 4):
  - Dry-run puro: no llama ``apply_reread``, no persiste bookkeeping, cero
    escrituras — ni siquiera bookkeeping (confirmado con ``session.expire`` +
    re-lectura desde la DB dentro de la misma transacción).
  - ``--record-scan``: persiste bookkeeping, ninguna tabla de negocio cambia.
  - ``--apply`` con outcome REAPPLIED sin ediciones: sí aplica (``DataRepairRun``
    creado, ``decision_audit_log`` recibe la entrada), ``ingestion_version``
    queda en ``INGESTION_VERSION``.
  - ``--apply`` con outcome FORCED_UNVERIFIED: NUNCA se aplica, aunque se pase
    --apply — la garantía explícita contra la corrección del plan original.
  - ``--apply`` con ``has_user_edits=True``: nunca se aplica.
  - Idempotencia: correr dos veces no duplica auditoría ni ``DataRepairRun``.
  - (fix round post-review) Aislamiento de errores por archivo en
    ``run_scan``: un archivo que revienta durante ``evaluate_file`` no aborta
    el resto del batch.
  - (fix round post-review, hallazgo Critical) Aislamiento de errores CON una
    query real de por medio antes del fallo (el escenario que el fake anterior
    no cubría — un ``rollback()`` sobre una sesión donde NO corrió ninguna
    query real es un no-op) y supervivencia de ``run_scan`` sin
    ``expire_on_commit=False`` explícito en la sesión del test (regresión de
    guardia si alguien vuelve a sacar el flag de ``main()``).
  - (fix round post-review, hallazgo Important #5) ``--skip-scanned`` excluye
    archivos con ``latest_preview_version >= to_version``; sin el flag, el
    comportamiento actual se preserva.

Nota sobre auditoría en SQLite: ``reanalyze_ingestion.py`` llama SIEMPRE al
helper real ``_db.py::insert_decision_audit`` (sin bifurcación por dialecto en
el módulo bajo test — ver fix round post-review, hallazgo 2). Ese helper usa
``gen_random_uuid()``/``now()`` de Postgres vía SQL crudo, que SQLite no
soporta, así que el fixture ``_patch_audit_insert`` (autouse, abajo) lo
reemplaza por un fake equivalente vía ``monkeypatch`` en TODA esta suite. Esto
elimina el código de producción test-only: no hay una segunda implementación
real del INSERT en ``reanalyze_ingestion.py`` que pueda divergir en silencio
del helper canónico si este cambia de columnas en el futuro.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.application.services import reread_service as reread_service_module
from app.application.services.column_risk import apply_column_risk_decisions
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    _capture_column_risk_rows,
    default_confirmed_fields,
    insert_confirmed_data,
)
from app.domain.ingestion_version import INGESTION_VERSION
from app.integrations.s3 import S3Client
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.schemas.ingestion import ColumnRiskDecision

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module() -> ModuleType:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "reanalyze_ingestion", _SCRIPTS_DIR / "reanalyze_ingestion.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registrar en sys.modules ANTES de ejecutar: el módulo usa `@dataclass` con
    # `from __future__ import annotations` (anotaciones diferidas a string) —
    # dataclasses.fields() necesita resolverlas vía `sys.modules[cls.__module__]`,
    # que solo existe si el módulo está registrado antes de exec_module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    return _load_module()


async def _sqlite_insert_decision_audit(
    session: AsyncSession,
    *,
    tenant_id: str,
    decision_type: str,
    decision_data: dict[str, Any],
    triggered_by: str,
) -> str:
    """Fake SQLite-compatible de ``_db.py::insert_decision_audit`` para esta
    suite (fix round post-review, hallazgo 2).

    El real usa ``gen_random_uuid()``/``now()`` de Postgres server-side vía SQL
    crudo — SQLite (el dialecto de ``db_session``, in-memory) no los soporta.
    En vez de mantener una segunda implementación REAL de este INSERT dentro
    de ``reanalyze_ingestion.py`` (el wrapper dialect-aware que tenía la
    primera versión de esta tarea, sin ningún test que atara ambos caminos),
    el módulo bajo test llama SIEMPRE al helper canónico — y acá lo
    reemplazamos por este fake vía ``monkeypatch`` (fixture
    ``_patch_audit_insert``, autouse). Mismo shape de columnas
    (id/tenant_id/decision_type/decision_data/triggered_by/created_at) que el
    real, con generación client-side de ``id``/``created_at``."""
    audit_id = uuid.uuid4()
    session.add(
        DecisionAuditLog(
            id=audit_id,
            tenant_id=uuid.UUID(tenant_id),
            decision_type=decision_type,
            decision_data=decision_data,
            triggered_by=triggered_by,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return str(audit_id)


@pytest.fixture(autouse=True)
def _patch_audit_insert(mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reemplaza, en TODOS los tests de este archivo, el ``insert_decision_audit``
    importado dentro del namespace de ``reanalyze_ingestion`` por el fake
    SQLite de arriba. Producción llama siempre al helper real de ``_db.py``
    (sin bifurcación) — este monkeypatch es puramente de test."""
    monkeypatch.setattr(mod, "insert_decision_audit", _sqlite_insert_decision_audit)


# ── fixtures (mismo patrón que app/tests/services/test_reread_file.py) ────────


@pytest_asyncio.fixture
async def tenant(db_session: AsyncSession) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Kiosco Reanalisis",
        display_name="Kiosco Reanalisis",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(t)
    await db_session.commit()
    return t


def _patch_s3(monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return content

    monkeypatch.setattr(S3Client, "download", _fake_download)


async def _make_file(session: AsyncSession, tenant_row: Tenant, content: bytes) -> UploadedFile:
    summary = parse_uploaded_content(content, "text/csv", "gastos.csv")
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant_row.tenant_id,
        uploaded_by=None,
        original_filename="gastos.csv",
        s3_key=f"tenants/{tenant_row.tenant_id}/gastos.csv",
        content_type="text/csv",
        size_bytes=len(content),
        purpose="gastos",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": default_confirmed_fields(summary),
        },
    )
    session.add(f)
    await session.commit()
    return f


# CSV donde la 2ª fila tiene "monto" vacío → ruteada a "Otros" por la decisión de
# riesgo persistida (simula el confirm F8b+). FIXED la corrige.
_CSV_RISK_BAD = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,1500,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,,Panaderia Norte\n"
)
_CSV_RISK_FIXED = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,1500,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,800,Panaderia Norte\n"
)
_RISK_DECISION = {
    "context_id": "table",
    "source_column": "monto",
    "target_field": "amount",
    "action": "route_affected_rows_to_others",
}

# Archivo pre-F8 (sin column_risk_decisions guardadas): "monto" 100% nulo, sin
# columna de reemplazo → única acción legal (route_affected_rows_to_others)
# pero sobre un mapeo re-derivado (guess) → outcome FORCED_UNVERIFIED.
_CSV_FORCED_UNVERIFIED = (
    b"fecha,producto,monto,proveedor\n"
    b"2026-01-05,Coca Cola,,Distribuidora Sur\n"
    b"2026-01-06,Pan Lactal,,Panaderia Norte\n"
)


async def _first_confirm_with_risk(
    session: AsyncSession, tenant_row: Tenant, content: bytes
) -> UploadedFile:
    """Simula el confirm original (F8b+): persiste la decisión de riesgo en el
    summary, importa solo las filas válidas y captura la afectada en "Otros"."""
    summary = parse_uploaded_content(content, "text/csv", "gastos.csv")
    confirmed = default_confirmed_fields(summary)
    file = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant_row.tenant_id,
        uploaded_by=None,
        original_filename="gastos.csv",
        s3_key=f"tenants/{tenant_row.tenant_id}/gastos.csv",
        content_type="text/csv",
        size_bytes=len(content),
        purpose="gastos",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": confirmed,
            "column_risk_decisions": [_RISK_DECISION],
        },
    )
    session.add(file)
    await session.commit()

    applied = apply_column_risk_decisions(summary, [ColumnRiskDecision(**_RISK_DECISION)], {})
    await insert_confirmed_data(
        session,
        tenant_row.tenant_id,
        applied.summary,
        confirmed,
        source="ingestion",
        uploaded_file_id=file.id,
    )
    for cid, rows_by_idx in applied.routed_rows.items():
        if rows_by_idx:
            await _capture_column_risk_rows(
                session,
                tenant_row.tenant_id,
                file.id,
                cid,
                applied.routed_entity.get(cid) or "otros",
                rows_by_idx,
                source="ingestion",
            )
    await session.commit()
    return file


async def _active_expenses(session: AsyncSession, file: UploadedFile) -> list[ExpenseEntry]:
    res = await session.execute(
        select(ExpenseEntry).where(
            ExpenseEntry.source_upload_id == file.id,
            ExpenseEntry.voided_at.is_(None),
        )
    )
    return list(res.scalars().all())


async def _audit_count(session: AsyncSession, decision_type: str) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(DecisionAuditLog)
        .where(DecisionAuditLog.decision_type == decision_type)
    )
    return int(count or 0)


async def _repair_run_count(session: AsyncSession) -> int:
    count = await session.scalar(select(func.count()).select_from(DataRepairRun))
    return int(count or 0)


# ── selección de candidatos ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_select_candidate_files_filters_by_version_window(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_RISK_FIXED)
    file = await _make_file(db_session, tenant, _CSV_RISK_FIXED)
    # Fuera de rango: ya está en la versión objetivo.
    file.ingestion_version = INGESTION_VERSION
    await db_session.commit()

    files = await mod.select_candidate_files(
        db_session, tenant_ids=[tenant.tenant_id], from_version=1, to_version=INGESTION_VERSION
    )
    assert file.id not in {f.id for f in files}

    file.ingestion_version = 1
    await db_session.commit()
    files = await mod.select_candidate_files(
        db_session, tenant_ids=[tenant.tenant_id], from_version=1, to_version=INGESTION_VERSION
    )
    assert file.id in {f.id for f in files}


@pytest.mark.asyncio
async def test_select_candidate_files_skip_scanned(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Important #5 (fix round post-review): ``--skip-scanned`` excluye
    archivos ya escaneados con la versión objetivo actual (``
    latest_preview_version >= to_version``) — evita re-descargar/re-parsear de
    S3 en cada corrida de ``--all-active`` archivos sin riesgo detectado (que
    nunca bumpean ``ingestion_version`` por diseño). Sin el flag, el
    comportamiento actual se preserva (el archivo sigue apareciendo)."""
    _patch_s3(monkeypatch, _CSV_FORCED_UNVERIFIED)
    file = await _make_file(db_session, tenant, _CSV_FORCED_UNVERIFIED)
    # ingestion_version sigue en 1 (nunca bumpea sin REAPPLIED), pero ya fue
    # escaneado con la versión objetivo actual.
    file.latest_preview_version = INGESTION_VERSION
    await db_session.commit()

    # Sin el flag: comportamiento actual preservado, el archivo aparece.
    files_default = await mod.select_candidate_files(
        db_session, tenant_ids=[tenant.tenant_id], from_version=1, to_version=INGESTION_VERSION
    )
    assert file.id in {f.id for f in files_default}

    # Con --skip-scanned: excluido, ya fue escaneado con la versión objetivo.
    files_skip = await mod.select_candidate_files(
        db_session,
        tenant_ids=[tenant.tenant_id],
        from_version=1,
        to_version=INGESTION_VERSION,
        skip_scanned=True,
    )
    assert file.id not in {f.id for f in files_skip}

    # Si `latest_preview_version` es None (nunca escaneado) sigue apareciendo
    # incluso con el flag.
    file.latest_preview_version = None
    await db_session.commit()
    files_skip_none = await mod.select_candidate_files(
        db_session,
        tenant_ids=[tenant.tenant_id],
        from_version=1,
        to_version=INGESTION_VERSION,
        skip_scanned=True,
    )
    assert file.id in {f.id for f in files_skip_none}


# ── dry-run puro: cero escrituras ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin --record-scan ni --apply: ni bookkeeping ni negocio se tocan."""
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    file = await _first_confirm_with_risk(db_session, tenant, _CSV_RISK_BAD)

    # Snapshot antes.
    before_version = file.ingestion_version
    before_latest_preview = file.latest_preview_version
    before_status = file.reread_status
    before_summary = file.reread_summary
    before_updated_at = file.updated_at
    before_expenses = len(await _active_expenses(db_session, file))
    before_audit = await _audit_count(db_session, mod.DECISION_TYPE_AUTO_APPLY)
    before_runs = await _repair_run_count(db_session)

    # Fila corregida en S3 → outcome sería REAPPLIED, elegible de no ser dry-run.
    _patch_s3(monkeypatch, _CSV_RISK_FIXED)
    s3 = S3Client()
    entry = await mod.evaluate_file(db_session, s3, file, do_apply=False)

    assert entry.bucket == mod.BUCKET_ELIGIBLE_NOT_APPLIED
    assert entry.applied is False

    # Re-lee desde la DB dentro de la MISMA transacción (refresh fuerza el
    # roundtrip): si algo se hubiera flusheado, esto lo revelaría.
    await db_session.refresh(file)
    assert file.ingestion_version == before_version
    assert file.latest_preview_version == before_latest_preview
    assert file.reread_status == before_status
    assert file.reread_summary == before_summary
    # SQLite no persiste tzinfo (el refresh vuelve naive) — comparar el valor
    # naive de ambos lados es lo que importa acá, no la representación.
    after_updated_at = file.updated_at
    assert after_updated_at.replace(tzinfo=None) == before_updated_at.replace(tzinfo=None)
    assert len(await _active_expenses(db_session, file)) == before_expenses
    assert await _audit_count(db_session, mod.DECISION_TYPE_AUTO_APPLY) == before_audit
    assert await _repair_run_count(db_session) == before_runs


# ── --record-scan: bookkeeping sí, negocio no ──────────────────────────────────


@pytest.mark.asyncio
async def test_record_scan_persists_bookkeeping_only(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_FORCED_UNVERIFIED)
    file = await _make_file(db_session, tenant, _CSV_FORCED_UNVERIFIED)
    before_version = file.ingestion_version
    before_expenses = len(await _active_expenses(db_session, file))
    before_runs = await _repair_run_count(db_session)

    s3 = S3Client()
    entry = await mod.evaluate_file(db_session, s3, file, do_apply=False)
    assert entry.bucket == mod.BUCKET_FORCED_UNVERIFIED

    mod.record_bookkeeping(file, entry)
    await db_session.commit()

    assert file.latest_preview_version == INGESTION_VERSION
    assert file.reread_status == mod.REREAD_STATUS_NEEDS_REVIEW
    assert file.reread_summary is not None
    assert file.reread_summary["outcome"] == "FORCED_UNVERIFIED"
    assert file.reread_summary["bucket"] == mod.BUCKET_FORCED_UNVERIFIED
    assert file.reread_summary["has_user_edits"] is False

    # Nada de negocio cambió.
    assert file.ingestion_version == before_version
    assert len(await _active_expenses(db_session, file)) == before_expenses
    assert await _repair_run_count(db_session) == before_runs


# ── --apply: solo REAPPLIED sin ediciones humanas ──────────────────────────────


@pytest.mark.asyncio
async def test_apply_reapplied_without_edits_applies_and_audits(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    file = await _first_confirm_with_risk(db_session, tenant, _CSV_RISK_BAD)
    assert len(await _active_expenses(db_session, file)) == 1  # 1 válida, 1 en Otros

    _patch_s3(monkeypatch, _CSV_RISK_FIXED)
    s3 = S3Client()
    entry = await mod.evaluate_file(db_session, s3, file, do_apply=True)

    assert entry.bucket == mod.BUCKET_APPLIED
    assert entry.applied is True
    assert entry.outcome == "REAPPLIED"

    mod.record_bookkeeping(file, entry)
    await db_session.commit()

    assert file.ingestion_version == INGESTION_VERSION
    assert len(await _active_expenses(db_session, file)) == 2  # la corregida entró
    assert await _repair_run_count(db_session) == 1
    assert await _audit_count(db_session, mod.DECISION_TYPE_AUTO_APPLY) == 1

    audit_row = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.decision_type == mod.DECISION_TYPE_AUTO_APPLY
            )
        )
    ).scalar_one()
    assert audit_row.decision_data["file_id"] == str(file.id)
    assert audit_row.decision_data["from_version"] == 1
    assert audit_row.decision_data["to_version"] == INGESTION_VERSION
    assert audit_row.triggered_by == mod.TRIGGERED_BY


@pytest.mark.asyncio
async def test_apply_forced_unverified_never_auto_applies(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garantía explícita: FORCED_UNVERIFIED (acción única, pero mapeo re-derivado
    no verificado) NUNCA se aplica, aunque se pase --apply."""
    _patch_s3(monkeypatch, _CSV_FORCED_UNVERIFIED)
    file = await _make_file(db_session, tenant, _CSV_FORCED_UNVERIFIED)
    before_expenses = len(await _active_expenses(db_session, file))
    before_runs = await _repair_run_count(db_session)
    before_audit = await _audit_count(db_session, mod.DECISION_TYPE_AUTO_APPLY)

    s3 = S3Client()
    entry = await mod.evaluate_file(db_session, s3, file, do_apply=True)

    assert entry.bucket == mod.BUCKET_FORCED_UNVERIFIED
    assert entry.outcome == "FORCED_UNVERIFIED"
    assert entry.applied is False

    assert file.ingestion_version == 1  # sin cambios — NUNCA se bumpea
    assert len(await _active_expenses(db_session, file)) == before_expenses
    assert await _repair_run_count(db_session) == before_runs
    assert await _audit_count(db_session, mod.DECISION_TYPE_AUTO_APPLY) == before_audit

    mod.record_bookkeeping(file, entry)
    await db_session.commit()
    assert file.reread_status == mod.REREAD_STATUS_NEEDS_REVIEW
    assert file.reread_summary["outcome"] == "FORCED_UNVERIFIED"


@pytest.mark.asyncio
async def test_apply_ambiguous_never_auto_applies(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mismo espíritu que FORCED_UNVERIFIED: AMBIGUOUS tampoco se aplica jamás."""
    csv_ambiguous = (
        b"fecha,producto,monto,importe,proveedor\n"
        b"2026-01-05,Coca Cola,1500,1500,Distribuidora Sur\n"
        b"2026-01-06,Pan Lactal,800,,Panaderia Norte\n"
    )
    _patch_s3(monkeypatch, csv_ambiguous)
    file = await _make_file(db_session, tenant, csv_ambiguous)

    s3 = S3Client()
    entry = await mod.evaluate_file(db_session, s3, file, do_apply=True)

    assert entry.bucket == mod.BUCKET_AMBIGUOUS
    assert entry.outcome == "AMBIGUOUS"
    assert entry.applied is False
    assert file.ingestion_version == 1
    assert await _repair_run_count(db_session) == 0
    assert await _audit_count(db_session, mod.DECISION_TYPE_AUTO_APPLY) == 0


@pytest.mark.asyncio
async def test_apply_with_user_edits_never_auto_applies(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REAPPLIED pero con has_user_edits=True: nunca se aplica."""
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    file = await _first_confirm_with_risk(db_session, tenant, _CSV_RISK_BAD)
    active = await _active_expenses(db_session, file)
    assert len(active) == 1
    active[0].has_user_edits = True
    await db_session.commit()

    _patch_s3(monkeypatch, _CSV_RISK_FIXED)
    s3 = S3Client()
    entry = await mod.evaluate_file(db_session, s3, file, do_apply=True)

    assert entry.bucket == mod.BUCKET_HAS_USER_EDITS
    assert entry.outcome == "REAPPLIED"
    assert entry.applied is False
    assert file.ingestion_version == 1
    assert await _repair_run_count(db_session) == 0
    assert await _audit_count(db_session, mod.DECISION_TYPE_AUTO_APPLY) == 0


# ── idempotencia ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_running_twice_does_not_duplicate_audit_or_runs(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correr el comando dos veces seguidas (selección + scan completo, como
    hace ``main()``) sobre el mismo set de archivos: la segunda corrida no debe
    generar nuevas filas en ``decision_audit_log`` ni ``DataRepairRun`` para el
    archivo ya resuelto en la primera — porque ya no lo selecciona (Task 4, ver
    docstring de ``run_scan``), NO porque ``evaluate_file`` sea idempotente por
    sí solo."""
    _patch_s3(monkeypatch, _CSV_RISK_BAD)
    file = await _first_confirm_with_risk(db_session, tenant, _CSV_RISK_BAD)

    _patch_s3(monkeypatch, _CSV_RISK_FIXED)
    s3 = S3Client()

    # Primera corrida completa (selección + scan + apply).
    files_run1 = await mod.select_candidate_files(
        db_session, tenant_ids=[tenant.tenant_id], from_version=1, to_version=INGESTION_VERSION
    )
    assert file.id in {f.id for f in files_run1}
    entries1 = await mod.run_scan(db_session, s3, files_run1, do_apply=True, record_scan=True)
    assert len(entries1) == 1
    assert entries1[0].applied is True

    await db_session.refresh(file)
    assert file.ingestion_version == INGESTION_VERSION
    assert await _repair_run_count(db_session) == 1
    assert await _audit_count(db_session, mod.DECISION_TYPE_AUTO_APPLY) == 1

    # Segunda corrida completa: el archivo ya no entra en el filtro de
    # selección (ingestion_version == to_version, fuera de `< to_version`) —
    # `run_scan` sobre una lista vacía es un no-op.
    files_run2 = await mod.select_candidate_files(
        db_session, tenant_ids=[tenant.tenant_id], from_version=1, to_version=INGESTION_VERSION
    )
    assert file.id not in {f.id for f in files_run2}
    entries2 = await mod.run_scan(db_session, s3, files_run2, do_apply=True, record_scan=True)
    assert entries2 == []

    assert await _audit_count(db_session, mod.DECISION_TYPE_AUTO_APPLY) == 1
    assert await _repair_run_count(db_session) == 1


# ── aislamiento de errores por archivo (fix round post-review, hallazgo 1) ────


@pytest.mark.asyncio
async def test_run_scan_isolates_per_file_errors(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un fallo puntual de I/O (ej. descarga S3) evaluando UN archivo no debe
    abortar el resto del batch: los demás candidatos se evalúan normalmente y
    el que falló aparece en ``BUCKET_ERROR`` con tenant/file_id/filename y el
    mensaje sanitizado, sin que la excepción se propague fuera de ``run_scan``.

    El archivo que falla va PRIMERO en la lista — así confirmamos que
    ``run_scan`` sigue procesando los archivos siguientes, no solo que no
    revienta con la lista completa."""
    _patch_s3(monkeypatch, _CSV_FORCED_UNVERIFIED)
    file_bad = await _make_file(db_session, tenant, _CSV_FORCED_UNVERIFIED)
    file_ok = await _make_file(db_session, tenant, _CSV_FORCED_UNVERIFIED)

    real_preview = reread_service_module.preview_reread

    async def _flaky_preview(
        session: AsyncSession,
        file_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        s3: S3Client | None = None,
    ) -> Any:
        if file_id == file_bad.id:
            raise RuntimeError("s3 download failed: /internal/bucket/path/object-not-found")
        return await real_preview(session, file_id, tenant_id, s3=s3)

    monkeypatch.setattr(reread_service_module, "preview_reread", _flaky_preview)

    s3 = S3Client()
    entries = await mod.run_scan(
        db_session, s3, [file_bad, file_ok], do_apply=False, record_scan=False
    )

    assert len(entries) == 2
    by_id = {e.file_id: e for e in entries}

    bad_entry = by_id[file_bad.id]
    assert bad_entry.bucket == mod.BUCKET_ERROR
    assert bad_entry.tenant_id == tenant.tenant_id
    assert bad_entry.filename == file_bad.original_filename
    assert bad_entry.applied is False
    assert bad_entry.exclusion_reason is not None
    assert "RuntimeError" in bad_entry.exclusion_reason
    assert "s3 download failed" in bad_entry.exclusion_reason

    # El archivo SIGUIENTE en la lista se evaluó normalmente pese al fallo del
    # anterior — esta es la garantía central del hallazgo 1.
    ok_entry = by_id[file_ok.id]
    assert ok_entry.bucket == mod.BUCKET_FORCED_UNVERIFIED
    assert ok_entry.outcome == "FORCED_UNVERIFIED"

    # La sesión quedó utilizable tras el rollback del fallo (evaluate_file de
    # file_ok pudo leer/flushear sin arrastrar el error del archivo anterior).
    await db_session.refresh(file_bad)
    await db_session.refresh(file_ok)


@pytest.mark.asyncio
async def test_run_scan_error_bucket_never_gets_bookkeeping(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con ``record_scan=True``, un archivo en ``BUCKET_ERROR`` no debe recibir
    bookkeeping (no completamos una evaluación que no terminó)."""
    _patch_s3(monkeypatch, _CSV_FORCED_UNVERIFIED)
    file_bad = await _make_file(db_session, tenant, _CSV_FORCED_UNVERIFIED)
    before_latest_preview = file_bad.latest_preview_version
    before_status = file_bad.reread_status

    async def _always_fails(
        session: AsyncSession,
        file_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        s3: S3Client | None = None,
    ) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(reread_service_module, "preview_reread", _always_fails)

    s3 = S3Client()
    entries = await mod.run_scan(
        db_session, s3, [file_bad], do_apply=False, record_scan=True
    )
    assert entries[0].bucket == mod.BUCKET_ERROR

    await db_session.refresh(file_bad)
    assert file_bad.latest_preview_version == before_latest_preview
    assert file_bad.reread_status == before_status


# ── regresión del hallazgo Critical (fix round post-review) ──────────────────
#
# Los dos fakes de arriba (``_flaky_preview``/``_always_fails``) lanzan la
# excepción ANTES de que ``preview_reread`` toque la sesión — precisamente lo
# que ocultaba el bug real: un ``session.rollback()`` sobre una sesión donde
# NO se ejecutó ninguna query real desde el último commit es un no-op (no
# expira nada). El bug real solo se manifestaba cuando la excepción ocurría
# DESPUÉS de al menos una query real (ej. el ``SELECT`` de ``_load_file``
# dentro de ``preview_reread``, que siempre corre primero) — un escenario
# realista (falla la descarga de S3 recién en el segundo paso). Los dos tests
# siguientes cubren exactamente eso.


@pytest.mark.asyncio
async def test_run_scan_isolates_per_file_errors_after_real_query(
    mod: ModuleType, db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diferencia de ``test_run_scan_isolates_per_file_errors``, este fake deja
    correr ``preview_reread`` REAL primero (dispara sus queries reales — el
    ``SELECT`` de ``_load_file`` entre otras) y RECIÉN DESPUÉS lanza la
    excepción. El ``session.rollback()`` que sigue en ``run_scan`` SÍ expira
    todos los objetos del identity map en este escenario (confirmado
    empíricamente) — exactamente el camino que el fix de ``session.get`` +
    ``inspect(...).identity`` en ``run_scan`` tiene que sobrevivir. Con 2
    archivos candidatos: el archivo 1 falla, el archivo 2 se evalúa
    correctamente pese al fallo del anterior."""
    _patch_s3(monkeypatch, _CSV_FORCED_UNVERIFIED)
    file_bad = await _make_file(db_session, tenant, _CSV_FORCED_UNVERIFIED)
    file_ok = await _make_file(db_session, tenant, _CSV_FORCED_UNVERIFIED)
    # Capturados ANTES de ``run_scan``: el ``rollback()`` que sigue a un fallo
    # DESPUÉS de una query real expira INCONDICIONALMENTE estos objetos (son
    # el mismo identity map que usa ``run_scan`` — misma sesión). Leerlos
    # DESPUÉS, en las aserciones de este test, sería exactamente el mismo bug
    # que ``run_scan`` tuvo que dejar de tener — este test no puede cometerlo
    # en su propio código de verificación.
    file_bad_id = file_bad.id
    file_ok_id = file_ok.id
    file_bad_filename = file_bad.original_filename
    tenant_id_value = tenant.tenant_id

    real_preview = reread_service_module.preview_reread

    async def _flaky_preview_after_real_query(
        session: AsyncSession,
        file_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        s3: S3Client | None = None,
    ) -> Any:
        if file_id == file_bad_id:
            # Dejamos correr la implementación REAL primero — hace queries de
            # verdad (``_load_file`` y las de estimación) — y RECIÉN DESPUÉS
            # fallamos, simulando un error tardío (ej. red) en la MISMA
            # evaluación. Esto es lo que el fake anterior NO cubría.
            await real_preview(session, file_id, tenant_id, s3=s3)
            raise RuntimeError("s3 download failed after a real query")
        return await real_preview(session, file_id, tenant_id, s3=s3)

    monkeypatch.setattr(reread_service_module, "preview_reread", _flaky_preview_after_real_query)

    s3 = S3Client()
    entries = await mod.run_scan(
        db_session, s3, [file_bad, file_ok], do_apply=False, record_scan=False
    )

    assert len(entries) == 2
    by_id = {e.file_id: e for e in entries}

    bad_entry = by_id[file_bad_id]
    assert bad_entry.bucket == mod.BUCKET_ERROR
    assert bad_entry.tenant_id == tenant_id_value
    assert bad_entry.filename == file_bad_filename
    assert bad_entry.applied is False
    assert bad_entry.exclusion_reason is not None
    assert "RuntimeError" in bad_entry.exclusion_reason

    # Garantía central: el archivo SIGUIENTE se evaluó normalmente pese al
    # rollback-con-expiración disparado por el fallo del anterior.
    ok_entry = by_id[file_ok_id]
    assert ok_entry.bucket == mod.BUCKET_FORCED_UNVERIFIED
    assert ok_entry.outcome == "FORCED_UNVERIFIED"


@pytest.mark.asyncio
async def test_run_scan_survives_without_expire_on_commit_false(
    mod: ModuleType,
    isolated_db_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresión de guardia contra revertir la Parte 1 del fix Critical: si
    alguien vuelve a construir la sesión de ``main()`` SIN
    ``expire_on_commit=False`` (el default de SQLAlchemy es ``True``), CUALQUIER
    commit a mitad del loop expira TODOS los objetos cargados en el identity
    map — no solo tras un error. A diferencia del fixture ``db_session`` (que
    fija ``expire_on_commit=False`` explícitamente para TODOS los tests), este
    test arma su PROPIA sesión sobre ``isolated_db_engine`` sin ese override,
    para que una regresión futura de ``run_scan`` (sacar el re-fetch
    defensivo) rompa acá aunque ``main()`` siga bien, y viceversa.

    Con 2 archivos y ``record_scan=True`` (dispara ``record_bookkeeping`` +
    commit tras CADA archivo): sin el re-fetch defensivo de ``run_scan``, el
    segundo archivo reventaría con ``MissingGreenlet`` al leer cualquier
    atributo (incluido el primary key) tras el commit del primero."""
    monkeypatch.setattr(mod, "insert_decision_audit", _sqlite_insert_decision_audit)
    _patch_s3(monkeypatch, _CSV_FORCED_UNVERIFIED)

    # IDs generados como variables Python planas ANTES de cualquier commit —
    # nunca se leen desde un objeto ORM post-commit en este test (a
    # diferencia de ``_make_file``, que asume ``expire_on_commit=False`` como
    # el resto de esta suite). Es la única forma de armar el fixture sin
    # pisar el propio escenario que este test quiere ejercitar.
    tenant_id_value = uuid.uuid4()
    file_a_id = uuid.uuid4()
    file_b_id = uuid.uuid4()

    summary = parse_uploaded_content(_CSV_FORCED_UNVERIFIED, "text/csv", "gastos.csv")
    confirmed = default_confirmed_fields(summary)

    def _build_file(file_id: uuid.UUID) -> UploadedFile:
        return UploadedFile(
            id=file_id,
            tenant_id=tenant_id_value,
            uploaded_by=None,
            original_filename="gastos.csv",
            s3_key=f"tenants/{tenant_id_value}/{file_id}.csv",
            content_type="text/csv",
            size_bytes=len(_CSV_FORCED_UNVERIFIED),
            purpose="gastos",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json={
                "inferred_type": summary.get("inferred_type"),
                "confirmed_fields": confirmed,
            },
        )

    async with AsyncSession(isolated_db_engine) as session:  # SIN expire_on_commit=False
        session.add(
            Tenant(
                tenant_id=tenant_id_value,
                legal_name="Kiosco Expire Test",
                display_name="Kiosco Expire Test",
                currency="ARS",
                pricing_reference_mode="MEP",
                status="ACTIVE",
            )
        )
        file_a = _build_file(file_a_id)
        file_b = _build_file(file_b_id)
        session.add(file_a)
        session.add(file_b)
        # UN solo commit para tenant + ambos archivos: con el default
        # ``expire_on_commit=True`` de esta sesión, esto ya deja file_a/file_b
        # expirados ANTES de que ``run_scan`` arranque — el escenario más
        # exigente posible para el re-fetch defensivo.
        await session.commit()

        s3 = S3Client()
        entries = await mod.run_scan(
            session, s3, [file_a, file_b], do_apply=False, record_scan=True
        )

        assert len(entries) == 2
        assert {e.file_id for e in entries} == {file_a_id, file_b_id}
        for e in entries:
            assert e.bucket == mod.BUCKET_FORCED_UNVERIFIED
            assert e.outcome == "FORCED_UNVERIFIED"

        # Bookkeeping realmente se persistió para AMBOS (no solo para el
        # primero antes de una posible expiración silenciosa del segundo).
        refreshed_a = await session.get(UploadedFile, file_a_id)
        refreshed_b = await session.get(UploadedFile, file_b_id)
        assert refreshed_a is not None and refreshed_a.latest_preview_version == INGESTION_VERSION
        assert refreshed_b is not None and refreshed_b.latest_preview_version == INGESTION_VERSION
