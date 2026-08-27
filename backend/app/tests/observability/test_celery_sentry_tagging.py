"""
`_extract_tenant_id` — el tag de Sentry por task tiene que salir del NOMBRE
del parámetro, nunca de la posición. Regresión: una regla "primer argumento
posicional" tageaba `tenant_id` con el email de
`notify_access_request_account_exists(self, email)` (PII sin scrubbing de
tags) y con el `run_id` de `reread_apply(run_id, file_id, tenant_id)`.
"""

from types import SimpleNamespace
from typing import Any

from app.jobs.celery_app import _extract_tenant_id


def _fake_task(run: Any) -> SimpleNamespace:
    return SimpleNamespace(run=run)


class TestExtractTenantId:
    def test_lo_toma_de_kwargs_si_esta_presente(self) -> None:
        def run(tenant_id: str) -> None:
            pass

        result = _extract_tenant_id(_fake_task(run), (), {"tenant_id": "t-1"})

        assert result == "t-1"

    def test_lo_ubica_por_nombre_aunque_no_sea_el_primer_positional(self) -> None:
        def run(run_id: str, file_id: str, tenant_id: str) -> None:
            pass

        result = _extract_tenant_id(_fake_task(run), ("run-1", "file-1", "tenant-1"), {})

        assert result == "tenant-1"

    def test_no_inventa_un_tenant_id_de_un_task_sin_ese_parametro(self) -> None:
        def run(email: str) -> None:
            pass

        result = _extract_tenant_id(_fake_task(run), ("cliente@example.com",), {})

        assert result is None

    def test_ninguno_si_task_no_tiene_run_inspeccionable(self) -> None:
        result = _extract_tenant_id(object(), ("x",), {})

        assert result is None
