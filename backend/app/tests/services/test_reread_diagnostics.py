"""Tests de `reread_diagnostics_service`: salud del worker/cola de relectura.

Caso real que motivó este módulo (cuenta ASTERIA, 2026-08): dos
``DataRepairRun`` de relectura quedaron en ``RUNNING`` con
``details_json["phase"] == "queued"`` para siempre — nadie los tomó, y nada lo
detectaba. Estos tests cubren: (1) el chequeo de workers/cola, (2) el conteo
de runs encolados hace demasiado sin que ningún worker los tomara.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_diagnostics_service as diag
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant


def _mock_inspect(pong: dict, active_queues: dict, error: str | None = None):
    def _fake(_timeout: float) -> tuple[dict, dict, str | None]:
        return pong, active_queues, error

    return _fake


async def test_all_healthy(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diag,
        "_inspect_celery",
        _mock_inspect(
            {"worker1@host": "pong"},
            {"worker1@host": [{"name": "ingestion"}, {"name": "default"}]},
        ),
    )
    result = await diag.run_reread_diagnostics(db_session)
    assert result["overall_ok"] is True
    by_name = {c["check"]: c for c in result["checks"]}
    assert by_name["workers_responding"]["ok"] is True
    assert by_name["ingestion_queue_consumed"]["ok"] is True
    assert by_name["no_stale_queued_runs"]["ok"] is True


async def test_no_workers_responding(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diag, "_inspect_celery", _mock_inspect({}, {}))
    result = await diag.run_reread_diagnostics(db_session)
    assert result["overall_ok"] is False
    by_name = {c["check"]: c for c in result["checks"]}
    assert by_name["workers_responding"]["ok"] is False
    assert by_name["workers_responding"]["severity"] == "error"
    assert "ingestion_queue_consumed" not in by_name  # no se evalúa sin workers


async def test_worker_responds_but_not_listening_ingestion_queue(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escenario real de ASTERIA: el broker responde, pero ningún worker tiene
    la cola `ingestion` entre sus colas activas — CELERY_QUEUES mal
    configurado, o el proceso worker está corriendo otro rol."""
    monkeypatch.setattr(
        diag,
        "_inspect_celery",
        _mock_inspect(
            {"worker1@host": "pong"},
            {"worker1@host": [{"name": "scores"}, {"name": "default"}]},
        ),
    )
    result = await diag.run_reread_diagnostics(db_session)
    assert result["overall_ok"] is False
    by_name = {c["check"]: c for c in result["checks"]}
    assert by_name["ingestion_queue_consumed"]["ok"] is False


async def test_inspect_error_is_reported_not_raised(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        diag, "_inspect_celery", _mock_inspect({}, {}, error="Connection refused")
    )
    result = await diag.run_reread_diagnostics(db_session)
    assert result["overall_ok"] is False
    by_name = {c["check"]: c for c in result["checks"]}
    assert "Connection refused" in by_name["workers_responding"]["detail"]


async def test_stuck_queued_run_is_counted(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        diag,
        "_inspect_celery",
        _mock_inspect(
            {"worker1@host": "pong"}, {"worker1@host": [{"name": "ingestion"}]}
        ),
    )
    old_enough = datetime.now(UTC) - timedelta(
        seconds=diag._QUEUED_TOO_LONG_SECONDS + 5
    )
    run = DataRepairRun(
        tenant_id=sample_tenant.tenant_id,
        repair_type="REREAD_FILE",
        status="RUNNING",
        dry_run=False,
        details_json={"file_id": str(uuid.uuid4()), "phase": "queued"},
        created_at=old_enough,
    )
    db_session.add(run)
    await db_session.commit()

    result = await diag.run_reread_diagnostics(db_session)
    by_name = {c["check"]: c for c in result["checks"]}
    # Severidad "warning": no tira overall_ok abajo si el resto está sano.
    assert by_name["no_stale_queued_runs"]["ok"] is False
    assert result["overall_ok"] is True


async def test_run_in_applying_phase_is_not_counted_as_stuck(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un run que el worker SÍ tomó (`phase="applying"`) no es un huérfano de
    cola — está siendo procesado, no hay que alertar sobre él acá."""
    monkeypatch.setattr(
        diag,
        "_inspect_celery",
        _mock_inspect(
            {"worker1@host": "pong"}, {"worker1@host": [{"name": "ingestion"}]}
        ),
    )
    old_enough = datetime.now(UTC) - timedelta(
        seconds=diag._QUEUED_TOO_LONG_SECONDS + 5
    )
    run = DataRepairRun(
        tenant_id=sample_tenant.tenant_id,
        repair_type="REREAD_FILE",
        status="RUNNING",
        dry_run=False,
        details_json={"file_id": str(uuid.uuid4()), "phase": "applying"},
        created_at=old_enough,
    )
    db_session.add(run)
    await db_session.commit()

    result = await diag.run_reread_diagnostics(db_session)
    by_name = {c["check"]: c for c in result["checks"]}
    assert by_name["no_stale_queued_runs"]["ok"] is True


async def test_check_ingestion_workers_available_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diag,
        "_inspect_celery",
        _mock_inspect(
            {"worker1@host": "pong"}, {"worker1@host": [{"name": "ingestion"}]}
        ),
    )
    assert await diag.check_ingestion_workers_available() is True


async def test_check_ingestion_workers_available_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diag, "_inspect_celery", _mock_inspect({"worker1@host": "pong"}, {})
    )
    assert await diag.check_ingestion_workers_available() is False


async def test_check_ingestion_workers_available_fail_open_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open: si el chequeo mismo falla, NO bloquea (`None`, no `False`) —
    un chequeo de salud que corta el flujo por su propia falla sería peor que
    no tenerlo."""
    monkeypatch.setattr(
        diag, "_inspect_celery", _mock_inspect({}, {}, error="timeout")
    )
    assert await diag.check_ingestion_workers_available() is None
