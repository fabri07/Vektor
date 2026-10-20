"""B·5a: «Otros» por grupo — resumen, vista previa y descarte del conjunto EXACTO.

El descarte por grupo no confía en el conteo: si entre la vista previa y la
confirmación salen dos filas y entran otras dos, el conteo es igual y el token
no. Se descarta exactamente lo que el usuario vio, o nada (409).
"""

from __future__ import annotations

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.file import UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)
from app.tests.conftest import _pin_window_key

_MOTIVO = "Fila de total de la planilla: no es una operación"


async def _archivo(db: AsyncSession, tid: uuid.UUID, nombre: str = "ventas.xlsx") -> uuid.UUID:
    up = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tid,
        original_filename=nombre,
        s3_key="k",
        content_type="text/csv",
        size_bytes=10,
        purpose="ingestion",
    )
    db.add(up)
    await db.flush()
    return up.id


def _rec(
    tid: uuid.UUID,
    file_id: uuid.UUID | None,
    *,
    label: str | None = _MOTIVO,
    ctx: str | None = "sheet:Ventas",
    entity: str | None = "sale",
    n: int = 0,
) -> UnclassifiedRecord:
    return UnclassifiedRecord(
        tenant_id=tid,
        uploaded_file_id=file_id,
        source="ingestion",
        context_label=label,
        context_id=ctx,
        headers=["detalle", "monto"],
        row_data={"detalle": f"Total {n}", "monto": "100", "__context__": ctx},
        suggested_entity=entity,
        status=UNCLASSIFIED_STATUS_PENDING,
    )


async def _sembrar(db: AsyncSession, tid: uuid.UUID) -> tuple[uuid.UUID, list[UnclassifiedRecord]]:
    file_id = await _archivo(db, tid)
    grupo = [_rec(tid, file_id, n=i) for i in range(3)]
    otros = [
        _rec(tid, file_id, label="Fila sin fecha reconocible", n=9),
        _rec(tid, None, ctx=None, label=None, entity=None, n=10),
    ]
    db.add_all(grupo + otros)
    await db.commit()
    return file_id, grupo


async def _resumen(client: AsyncClient, headers: dict[str, str]) -> list[dict[str, Any]]:
    resp = await client.get("/api/v1/others/summary", headers=headers)
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def _grupo_total(grupos: list[dict[str, Any]]) -> dict[str, Any]:
    return next(g for g in grupos if g["motivo"] == _MOTIVO)


async def test_resumen_agrupa_por_archivo_hoja_motivo_y_sugerencia(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    file_id, _ = await _sembrar(db_session, sample_tenant.tenant_id)
    grupos = await _resumen(client, auth_headers)
    assert sorted(g["count"] for g in grupos) == [1, 1, 3]
    total = _grupo_total(grupos)
    assert total["uploaded_file_id"] == str(file_id)
    assert total["file_name"] == "ventas.xlsx"
    assert total["context_id"] == "sheet:Ventas"
    # La captura sin archivo, sin hoja ni sugerencia es su propio grupo.
    assert any(g["uploaded_file_id"] is None and g["motivo"] is None for g in grupos)

    # El listado acepta la clave del grupo y devuelve sólo sus filas.
    resp = await client.get(
        "/api/v1/others", params={"group": total["group_key"]}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert {r["context_label"] for r in resp.json()} == {_MOTIVO}
    assert len(resp.json()) == 3


async def test_descarta_exactamente_el_grupo_y_audita(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    await _sembrar(db_session, tid)
    key = _grupo_total(await _resumen(client, auth_headers))["group_key"]
    prev = await client.post(
        "/api/v1/others/dismiss-group/preview", json={"group_key": key}, headers=auth_headers
    )
    assert prev.status_code == 200
    assert prev.json()["count"] == 3
    assert len(prev.json()["samples"]) == 3
    assert all(not k.startswith("__") for s in prev.json()["samples"] for k in s)

    resp = await client.post(
        "/api/v1/others/dismiss-group",
        json={"group_key": key, "group_token": prev.json()["group_token"]},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"dismissed": 3}
    await db_session.commit()

    estados = {
        (r.context_label, r.status)
        for r in (await db_session.execute(select(UnclassifiedRecord))).scalars()
    }
    assert (_MOTIVO, UNCLASSIFIED_STATUS_DISMISSED) in estados
    # Los otros grupos siguen pendientes.
    assert ("Fila sin fecha reconocible", UNCLASSIFIED_STATUS_PENDING) in estados
    assert (None, UNCLASSIFIED_STATUS_PENDING) in estados
    audit = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.decision_type == "OTHERS_GROUP_DISMISSED"
            )
        )
    ).scalar_one()
    assert audit.decision_data["cantidad"] == 3


async def test_mismo_conteo_otro_conjunto_da_409_y_no_toca_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    file_id, grupo = await _sembrar(db_session, tid)
    key = _grupo_total(await _resumen(client, auth_headers))["group_key"]
    prev = await client.post(
        "/api/v1/others/dismiss-group/preview", json={"group_key": key}, headers=auth_headers
    )
    token = prev.json()["group_token"]

    # Entre la vista previa y el descarte: sale una fila (otra sesión la importó)
    # y entra otra del mismo grupo (una relectura). El conteo sigue en 3.
    grupo[0].status = "IMPORTED"
    db_session.add(_rec(tid, file_id, n=99))
    await db_session.commit()

    resp = await client.post(
        "/api/v1/others/dismiss-group",
        json={"group_key": key, "group_token": token},
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "GROUP_CHANGED"
    pendientes = (
        await db_session.execute(
            select(UnclassifiedRecord).where(
                UnclassifiedRecord.status == UNCLASSIFIED_STATUS_PENDING,
                UnclassifiedRecord.context_label == _MOTIVO,
            )
        )
    ).scalars().all()
    assert len(pendientes) == 3


async def test_otro_tenant_no_ve_ni_descarta_el_grupo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    second_auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _sembrar(db_session, sample_tenant.tenant_id)
    key = _grupo_total(await _resumen(client, auth_headers))["group_key"]
    prev = await client.post(
        "/api/v1/others/dismiss-group/preview", json={"group_key": key}, headers=auth_headers
    )
    assert await _resumen(client, second_auth_headers) == []
    ajena = await client.post(
        "/api/v1/others/dismiss-group/preview",
        json={"group_key": key},
        headers=second_auth_headers,
    )
    assert ajena.json()["count"] == 0
    resp = await client.post(
        "/api/v1/others/dismiss-group",
        json={"group_key": key, "group_token": prev.json()["group_token"]},
        headers=second_auth_headers,
    )
    assert resp.status_code == 409


async def test_viewer_403_y_sin_pin_428(
    client: AsyncClient,
    auth_headers: dict[str, str],
    viewer_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: Any,
    fake_redis: Any,
) -> None:
    await _sembrar(db_session, sample_tenant.tenant_id)
    key = _grupo_total(await _resumen(client, auth_headers))["group_key"]
    body = {"group_key": key, "group_token": "0" * 64}
    viewer = await client.post("/api/v1/others/dismiss-group", json=body, headers=viewer_headers)
    assert viewer.status_code == 403
    await fake_redis.delete(_pin_window_key(sample_tenant.tenant_id, sample_user.user_id))
    sin_pin = await client.post("/api/v1/others/dismiss-group", json=body, headers=auth_headers)
    assert sin_pin.status_code == 428


async def test_clave_invalida_422(client: AsyncClient, auth_headers: dict[str, str]) -> None:
    resp = await client.post(
        "/api/v1/others/dismiss-group/preview",
        json={"group_key": "no-es-una-clave"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_descarte_de_a_uno_queda_auditado(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    _, grupo = await _sembrar(db_session, sample_tenant.tenant_id)
    resp = await client.post(f"/api/v1/others/{grupo[0].id}/dismiss", headers=auth_headers)
    assert resp.status_code == 200
    audit = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.decision_type == "OTHERS_RECORD_DISMISSED"
            )
        )
    ).scalar_one()
    assert audit.decision_data["record_id"] == str(grupo[0].id)
