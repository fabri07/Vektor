from __future__ import annotations

import uuid
from typing import Any

import pytest


class _FakeEngine:
    async def dispose(self) -> None:
        pass


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def commit(self) -> None:
        pass


def _fake_factory() -> _FakeSession:
    return _FakeSession()


def test_ingestion_error_logs_tenant_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.jobs.ingestion_worker as worker

    captured: dict[str, Any] = {}
    tenant_id = str(uuid.uuid4())

    async def fail_load(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("load failed")

    def capture_error(event: str, **kwargs: Any) -> None:
        captured["event"] = event
        captured.update(kwargs)

    monkeypatch.setattr(
        worker,
        "_build_async_session",
        lambda database_url: (_FakeEngine(), _fake_factory),
    )
    monkeypatch.setattr(worker, "_load_and_lock", fail_load)
    monkeypatch.setattr(worker.logger, "error", capture_error)

    with pytest.raises(RuntimeError, match="load failed"):
        worker.process_spreadsheet(str(uuid.uuid4()), tenant_id)

    assert captured["event"] == "ingestion.spreadsheet.failed"
    assert captured["tenant_id"] == tenant_id
