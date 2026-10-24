"""Cargar datos por archivo exige el rol que da de alta una venta (I3-3).

Revisión profunda 2026-10-24: el router de ingesta no pedía rol, así que un
VIEWER (o un ANALYST) podía subir un archivo, confirmarlo —insertando ventas,
gastos, stock y maestros y consumiendo el cupo—, registrar una importación
asíncrona, cancelar, reprocesar y borrar mapeos aprendidos. `POST /sales` pide
OWNER/ADMIN; ahora la ingesta también, y el 403 sale antes de tocar nada.
"""

from __future__ import annotations

import unittest.mock
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.tests.api.v1 import test_ingestion as _ingesta
from app.utils.security import create_access_token

# Fixtures de los tests de ingesta: el mismo archivo listo para confirmar.
confirmed_file = _ingesta.confirmed_file
mock_s3_upload = _ingesta.mock_s3_upload
mock_spreadsheet_delay = _ingesta.mock_spreadsheet_delay
xlsx_bytes = _ingesta.xlsx_bytes

_CONFIRM_BODY: dict[str, Any] = {"confirmed_fields": {"ventas": True, "gastos": False}}


async def _headers_de(db: AsyncSession, tenant: Tenant, rol: str) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"{rol.lower()}-{uuid.uuid4().hex[:6]}@kiosco.com",
        full_name=f"Usuario {rol}",
        password_hash="x",
        role_code=rol,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": rol}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(params=["VIEWER", "ANALYST"])
async def sin_permiso(
    request: pytest.FixtureRequest, db_session: AsyncSession, sample_tenant: Tenant
) -> dict[str, str]:
    return await _headers_de(db_session, sample_tenant, str(request.param))


async def _ventas(db: AsyncSession, tenant: Tenant) -> int:
    return int(
        (
            await db.execute(
                select(func.count(SaleEntry.id)).where(
                    SaleEntry.tenant_id == tenant.tenant_id
                )
            )
        ).scalar_one()
    )


async def test_no_puede_confirmar_ni_se_escribe_nada(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sin_permiso: dict[str, str],
    confirmed_file: UploadedFile,
    mock_score_trigger: unittest.mock.MagicMock,
) -> None:
    antes = await _ventas(db_session, sample_tenant)
    resp = await client.post(
        f"/api/v1/ingestion/files/{confirmed_file.id}/confirm",
        headers=sin_permiso,
        json=_CONFIRM_BODY,
    )
    assert resp.status_code == 403, resp.text
    assert await _ventas(db_session, sample_tenant) == antes
    await db_session.refresh(confirmed_file)
    assert confirmed_file.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION


async def test_no_puede_registrar_una_importacion_asincrona(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sin_permiso: dict[str, str],
    confirmed_file: UploadedFile,
) -> None:
    resp = await client.post(
        f"/api/v1/ingestion/files/{confirmed_file.id}/imports",
        headers=sin_permiso,
        json={**_CONFIRM_BODY, "client_import_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 403, resp.text
    await db_session.refresh(confirmed_file)
    assert confirmed_file.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION


async def test_no_puede_subir_un_archivo(
    client: AsyncClient,
    sin_permiso: dict[str, str],
    xlsx_bytes: bytes,
    mock_s3_upload: unittest.mock.AsyncMock,
    mock_spreadsheet_delay: unittest.mock.MagicMock,
) -> None:
    resp = await client.post(
        "/api/v1/ingestion/upload",
        headers=sin_permiso,
        files={"file": ("ventas.xlsx", xlsx_bytes, "application/octet-stream")},
        params={"file_hint": "ventas"},
    )
    assert resp.status_code == 403, resp.text
    mock_s3_upload.assert_not_called()
    mock_spreadsheet_delay.assert_not_called()


async def test_no_puede_cancelar_ni_reprocesar(
    client: AsyncClient,
    db_session: AsyncSession,
    sin_permiso: dict[str, str],
    confirmed_file: UploadedFile,
) -> None:
    for accion in ("cancel", "reprocess"):
        resp = await client.post(
            f"/api/v1/ingestion/files/{confirmed_file.id}/{accion}", headers=sin_permiso
        )
        assert resp.status_code == 403, (accion, resp.text)
    await db_session.refresh(confirmed_file)
    assert confirmed_file.processing_status == PROCESSING_STATUS_NEEDS_CONFIRMATION


async def test_no_puede_borrar_un_mapeo_aprendido(
    client: AsyncClient, sin_permiso: dict[str, str]
) -> None:
    resp = await client.delete(
        f"/api/v1/ingestion/column-mappings/{uuid.uuid4()}", headers=sin_permiso
    )
    # 403 aunque el mapeo no exista: el permiso se mira antes que el recurso.
    assert resp.status_code == 403, resp.text


async def test_puede_seguir_mirando_lo_cargado(
    client: AsyncClient, sin_permiso: dict[str, str], confirmed_file: UploadedFile
) -> None:
    """Leer no cambia: el listado y el detalle siguen abiertos para el rol."""
    assert (await client.get("/api/v1/ingestion/files", headers=sin_permiso)).status_code == 200
    detalle = await client.get(
        f"/api/v1/ingestion/files/{confirmed_file.id}", headers=sin_permiso
    )
    assert detalle.status_code == 200


async def test_un_admin_sigue_pudiendo_confirmar(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    confirmed_file: UploadedFile,
    mock_score_trigger: unittest.mock.MagicMock,
) -> None:
    admin = await _headers_de(db_session, sample_tenant, "ADMIN")
    antes = await _ventas(db_session, sample_tenant)
    resp = await client.post(
        f"/api/v1/ingestion/files/{confirmed_file.id}/confirm",
        headers=admin,
        json=_CONFIRM_BODY,
    )
    assert resp.status_code == 200, resp.text
    assert await _ventas(db_session, sample_tenant) == antes + 1


async def test_sin_permiso_es_403_aunque_la_cuenta_este_en_mantenimiento(
    client: AsyncClient,
    sin_permiso: dict[str, str],
    confirmed_file: UploadedFile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El permiso se mira primero: 403, no el 423 de la deduplicación."""
    from app.application.services import maintenance_lock_service  # noqa: PLC0415

    monkeypatch.setattr(
        maintenance_lock_service, "is_locked", unittest.mock.AsyncMock(return_value=True)
    )
    resp = await client.post(
        f"/api/v1/ingestion/files/{confirmed_file.id}/confirm",
        headers=sin_permiso,
        json=_CONFIRM_BODY,
    )
    assert resp.status_code == 403, resp.text


async def test_un_mapeo_aprendido_real_no_se_borra(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sin_permiso: dict[str, str],
) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415

    from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

    ahora = datetime.now(UTC)
    mapeo = TenantColumnMapping(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        entity_type="sale",
        source_column="fecha",
        target_field="transaction_date",
        confirmed_count=1,
        last_seen_at=ahora,
        created_at=ahora,
    )
    db_session.add(mapeo)
    await db_session.commit()
    resp = await client.delete(
        f"/api/v1/ingestion/column-mappings/{mapeo.id}", headers=sin_permiso
    )
    assert resp.status_code == 403, resp.text
    assert await db_session.get(TenantColumnMapping, mapeo.id) is not None


# ── Barrido: una ruta nueva de ingesta no nace abierta ───────────────────────

#: Rutas de ingesta que no son GET y sin embargo no escriben: calculan un
#: preview a pedido de la pantalla. Cualquier otra ruta no-GET tiene que pedir
#: rol (`require_role` sin VIEWER/ANALYST) o `require_modify_access`.
_RUTAS_DE_SOLO_CALCULO = frozenset(
    {
        ("POST", "/api/v1/ingestion/files/{file_id}/column-risk"),
        ("POST", "/api/v1/ingestion/files/{file_id}/inventory-effects"),
        ("POST", "/api/v1/ingestion/files/{file_id}/purchase-groups"),
    }
)


def _permiso_de(call: Any) -> tuple[str, ...] | None:
    """Los roles que deja pasar una dependencia de permiso, o None si no lo es."""
    from app.api.v1.deps import require_modify_access  # noqa: PLC0415

    if call is require_modify_access:
        return ("OWNER", "sub-cuenta con can_modify_sensitive")
    if getattr(call, "__qualname__", "") == "require_role.<locals>._check":
        (celda,) = call.__closure__
        return tuple(celda.cell_contents)
    return None


def test_toda_ruta_de_ingesta_que_escribe_pide_permiso_primero() -> None:
    from fastapi.routing import APIRoute  # noqa: PLC0415

    from app.main import create_app  # noqa: PLC0415

    rutas = [
        r
        for r in create_app().routes
        if isinstance(r, APIRoute) and r.path.startswith("/api/v1/ingestion/")
    ]
    sin_permiso_primero = []
    for route in rutas:
        for metodo in route.methods - {"GET", "HEAD"}:
            if (metodo, route.path) in _RUTAS_DE_SOLO_CALCULO:
                continue
            permisos = [
                _permiso_de(d.call) for d in route.dependant.dependencies
            ]
            hay = [p for p in permisos if p is not None]
            assert hay, f"{metodo} {route.path} no pide permiso"
            assert not {"VIEWER", "ANALYST"} & set(hay[0]), f"{metodo} {route.path}"
            # Con dependencias de decorador (423, 402), el permiso va antes.
            decorador = route.dependencies
            if decorador and _permiso_de(decorador[0].dependency) is None and any(
                _permiso_de(d.dependency) is not None for d in decorador
            ):
                sin_permiso_primero.append(f"{metodo} {route.path}")
    assert not sin_permiso_primero, sin_permiso_primero
    assert {r.path for r in rutas} >= {p for _, p in _RUTAS_DE_SOLO_CALCULO}
