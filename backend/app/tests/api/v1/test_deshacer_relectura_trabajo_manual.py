"""Deshacer una relectura no pisa el trabajo manual ni deja «Otros» repetido (I4-3, I4-4).

Revisión profunda, etapa 2.

**I4-3.** ``undo_reread`` borraba (``delete`` Core) todas las ventas y gastos que
la relectura insertó, sin mirar ``has_user_edits``: un gasto que el usuario
corrigió a mano DESPUÉS de releer desaparecía y volvía el original con los
valores viejos, sin aviso. Decisión conservadora: el deshacer se FRENA entero
—409 ``UNDO_PISA_TRABAJO_MANUAL`` con conteo y muestras, nada escrito— en vez
de elegir por el usuario entre su corrección y el archivo.

**I4-4.** El deshacer no tocaba «Otros»: lo que la relectura mandó a la bandeja
seguía pendiente al lado del gasto original des-anulado (clasificarlo lo cargaba
dos veces), y los pendientes que la relectura descartó no volvían. Ahora el run
guarda los ids que creó y los que descartó: deshacer borra los primeros y
devuelve los segundos a pendientes. Una fila creada que el usuario ya clasificó
frena el deshacer con el mismo 409 (su gasto quedaría junto al original).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.file_deletion_service import (
    build_master_details,
    record_import_ledger,
    snapshot_masters_before_import,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.ingestion_version import INGESTION_VERSION
from app.integrations.s3 import S3Client
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_DISMISSED,
    UNCLASSIFIED_STATUS_IMPORTED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)
from app.tests.integration import _reread_scenario as esc

_FILAS = [
    esc.fila(5, esc.PRODUCTO, "2", "2400"),
    esc.fila(6, "Vela vainilla 200g", "3", "3600"),
]
#: Lo que devuelve «volver a leer». Lo cambia cada test.
_RELEIDO: dict[str, Any] = {"summary": esc.summary(_FILAS)}


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker en tests: ver el mismo fixture en ``test_reread_file.py``."""


@pytest.fixture(autouse=True)
def _s3_con_la_relectura(monkeypatch: pytest.MonkeyPatch) -> None:
    _RELEIDO["summary"] = esc.summary(_FILAS)

    async def _fresh(_file: UploadedFile, _s3: S3Client) -> dict[str, Any]:
        return _RELEIDO["summary"]

    async def _head(self: S3Client, key: str) -> dict[str, Any]:  # noqa: ARG001
        return {"etag": '"fijo"', "size": 1, "last_modified": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(reread_service, "_fresh_summary", _fresh)
    monkeypatch.setattr(S3Client, "head", _head)


async def _importar(s: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """Archivo de compras confirmado como el confirm real: efectos + ledger."""
    file_id = uuid.uuid4()
    s.add(
        UploadedFile(
            id=file_id,
            tenant_id=tenant_id,
            original_filename="compras.xlsx",
            s3_key=f"tenants/{tenant_id}/compras.xlsx",
            content_type="application/vnd.ms-excel",
            size_bytes=1,
            purpose="gastos",
            processing_status=PROCESSING_STATUS_DONE,
            parsed_summary_json=esc.summary(_FILAS),
            ingestion_version=INGESTION_VERSION,
        )
    )
    await s.commit()
    antes_c, antes_p = await snapshot_masters_before_import(s, tenant_id)
    counts = await insert_confirmed_data(
        s,
        tenant_id,
        esc.summary(_FILAS),
        {"gastos": True},
        return_details=True,
        uploaded_file_id=file_id,
    )
    detalles = counts.pop("product_details", []) or []
    await record_import_ledger(
        s,
        tenant_id=tenant_id,
        file_id=file_id,
        product_details=detalles,
        master_details=await build_master_details(s, counts, antes_c, antes_p),
    )
    await s.commit()
    return file_id


async def _gastos_vivos(s: AsyncSession, file_id: uuid.UUID) -> dict[str, str]:
    """``{id: monto}`` de los gastos vivos del archivo."""
    s.expire_all()
    filas = (
        await s.execute(
            select(ExpenseEntry.id, ExpenseEntry.amount).where(
                ExpenseEntry.source_upload_id == file_id, ExpenseEntry.voided_at.is_(None)
            )
        )
    ).all()
    return {str(i): str(m) for i, m in filas}


async def _otros(s: AsyncSession, file_id: uuid.UUID) -> dict[str, str]:
    """``{id: status}`` de «Otros» del archivo."""
    s.expire_all()
    filas = (
        await s.execute(
            select(UnclassifiedRecord.id, UnclassifiedRecord.status).where(
                UnclassifiedRecord.uploaded_file_id == file_id
            )
        )
    ).all()
    return {str(i): st for i, st in filas}


async def _releer(s: AsyncSession, file_id: uuid.UUID, tenant_id: uuid.UUID) -> uuid.UUID:
    resultado = await reread_service.apply_reread(
        s, file_id, tenant_id, accept_data_loss=True
    )
    await s.commit()
    return resultado.run_id


async def _gasto_insertado(s: AsyncSession, run_id: uuid.UUID) -> str:
    """El id del gasto que la relectura insertó (el de la fila corregida)."""
    run = await s.get(DataRepairRun, run_id)
    assert run is not None
    from app.persistence.models.repair import DataRepairItem  # noqa: PLC0415

    items = (
        await s.execute(
            select(DataRepairItem).where(
                DataRepairItem.run_id == run_id,
                DataRepairItem.action == reread_service.ACTION_INSERT,
            )
        )
    ).scalars()
    ids = [
        str((it.after_json or {})["id"])
        for it in items
        if (it.after_json or {}).get("kind") == "expense"
    ]
    assert ids, "la relectura tenía que insertar al menos un gasto"
    return ids[0]


async def _run(s: AsyncSession, run_id: uuid.UUID) -> DataRepairRun:
    s.expire_all()
    run = await s.get(DataRepairRun, run_id)
    assert run is not None
    return run


def _es_trabajo_manual(r: Any, *, gastos: int = 0, otros: int = 0) -> dict[str, Any]:
    assert r.status_code == 409, r.text
    detalle = r.json()["detail"]
    assert detalle["code"] == reread_service.UNDO_PISA_TRABAJO_MANUAL
    assert (detalle["ventas"], detalle["gastos"], detalle["otros"]) == (0, gastos, otros)
    assert detalle["muestras"], "el 409 trae muestras"
    assert "No se cambió nada" in detalle["message"]
    return detalle


# ── I4-3: lo corregido a mano después de releer ────────────────────────────────


async def test_deshacer_con_un_gasto_corregido_despues_de_releer_da_409_y_no_toca_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid)
    _RELEIDO["summary"] = esc.summary([esc.fila(5, esc.PRODUCTO, "2", "2500"), _FILAS[1]])
    run_id = await _releer(db_session, fid, tid)
    gasto_id = await _gasto_insertado(db_session, run_id)
    # Una compra de mercadería sólo deja editar descripción y notas; alcanza: la
    # corrección es del usuario igual (``has_user_edits``).
    r = await client.patch(
        f"/api/v1/expenses/{gasto_id}",
        json={"description": "Velas, corregido a mano"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    await db_session.commit()
    antes = await _gastos_vivos(db_session, fid)
    assert gasto_id in antes

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)

    detalle = _es_trabajo_manual(r, gastos=1)
    assert detalle["muestras"][0] == {
        "kind": "expense",
        "id": gasto_id,
        "fecha": "2024-03-05",
        "monto": "2500.00",
    }
    assert "corregiste a mano" in detalle["message"]
    # Nada escrito: la corrección sigue, el original sigue anulado, el run aplicado.
    assert await _gastos_vivos(db_session, fid) == antes
    gasto = await db_session.get(ExpenseEntry, uuid.UUID(gasto_id))
    assert gasto is not None and gasto.description == "Velas, corregido a mano"
    assert (await _run(db_session, run_id)).status == "APPLIED"


async def test_deshacer_sin_correcciones_posteriores_sigue_andando(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid)
    originales = await _gastos_vivos(db_session, fid)
    _RELEIDO["summary"] = esc.summary([esc.fila(5, esc.PRODUCTO, "2", "2500"), _FILAS[1]])
    run_id = await _releer(db_session, fid, tid)
    assert await _gastos_vivos(db_session, fid) != originales

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)

    assert r.status_code == 200, r.text
    assert await _gastos_vivos(db_session, fid) == originales
    assert (await _run(db_session, run_id)).status == "REVERTED"


# ── I4-4: «Otros» ──────────────────────────────────────────────────────────────


async def test_deshacer_borra_lo_que_la_relectura_mando_a_otros(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La relectura ya no sabe leer la fecha de una fila: su gasto se anula y la
    fila va a «Otros». Deshacer devuelve el gasto y saca la fila de la bandeja."""
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid)
    originales = await _gastos_vivos(db_session, fid)
    ilegible = {**_FILAS[1], "fecha": "sin fecha"}
    _RELEIDO["summary"] = esc.summary([_FILAS[0], ilegible])
    await _releer(db_session, fid, tid)
    creados = await _otros(db_session, fid)
    assert list(creados.values()) == [UNCLASSIFIED_STATUS_PENDING], creados
    assert len(await _gastos_vivos(db_session, fid)) == 1

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)

    assert r.status_code == 200, r.text
    assert await _gastos_vivos(db_session, fid) == originales
    assert await _otros(db_session, fid) == {}, "la copia pendiente duplicaba el gasto"


async def test_deshacer_con_una_fila_de_otros_ya_clasificada_da_409_y_no_toca_nada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid)
    ilegible = {**_FILAS[1], "fecha": "sin fecha"}
    _RELEIDO["summary"] = esc.summary([_FILAS[0], ilegible])
    run_id = await _releer(db_session, fid, tid)
    (otro_id,) = (await _otros(db_session, fid)).keys()
    # Clasificada después de releer (el estado que deja «Otros» al importarla).
    fila = await db_session.get(UnclassifiedRecord, uuid.UUID(otro_id))
    assert fila is not None
    fila.status = UNCLASSIFIED_STATUS_IMPORTED
    await db_session.commit()
    vivos = await _gastos_vivos(db_session, fid)

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)

    detalle = _es_trabajo_manual(r, otros=1)
    assert detalle["muestras"][0]["kind"] == "otros"
    assert detalle["muestras"][0]["id"] == otro_id
    assert "ya clasificaste" in detalle["message"]
    assert await _otros(db_session, fid) == {otro_id: UNCLASSIFIED_STATUS_IMPORTED}
    assert await _gastos_vivos(db_session, fid) == vivos
    assert (await _run(db_session, run_id)).status == "APPLIED"


async def test_deshacer_devuelve_a_pendientes_lo_que_la_relectura_descarto(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un pendiente de una hoja que la relectura lee como resumen se descarta;
    deshacer la relectura lo devuelve a la bandeja. Uno que el usuario descartó
    por su cuenta antes de releer no se toca."""
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid)
    pendiente = UnclassifiedRecord(
        tenant_id=tid,
        uploaded_file_id=fid,
        source="ingestion",
        context_label="Resumen",
        context_id="resumen",
        headers=["concepto", "total"],
        row_data={"concepto": "Total marzo", "total": "6000"},
    )
    ya_descartado = UnclassifiedRecord(
        tenant_id=tid,
        uploaded_file_id=fid,
        source="ingestion",
        context_label="Resumen",
        context_id="resumen",
        headers=["concepto", "total"],
        row_data={"concepto": "Total febrero", "total": "5000"},
        status=UNCLASSIFIED_STATUS_DISMISSED,
    )
    db_session.add_all([pendiente, ya_descartado])
    await db_session.flush()
    pendiente_id, ya_descartado_id = str(pendiente.id), str(ya_descartado.id)
    await db_session.commit()
    releido = esc.summary(_FILAS)
    releido["mapping_contexts"] = [
        {
            "context_id": "resumen",
            "label": "Resumen",
            "entity_type": None,
            "is_summary_or_derived": True,
            "headers": ["concepto", "total"],
            "preview_rows": [],
            "row_count": 1,
        }
    ]
    _RELEIDO["summary"] = releido
    await _releer(db_session, fid, tid)
    assert (await _otros(db_session, fid))[pendiente_id] == UNCLASSIFIED_STATUS_DISMISSED

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)

    assert r.status_code == 200, r.text
    assert await _otros(db_session, fid) == {
        pendiente_id: UNCLASSIFIED_STATUS_PENDING,
        ya_descartado_id: UNCLASSIFIED_STATUS_DISMISSED,
    }
    fila = await db_session.get(UnclassifiedRecord, uuid.UUID(pendiente_id))
    assert fila is not None and fila.resolved_at is None


async def test_un_run_anterior_a_i4_4_se_deshace_sin_tocar_otros(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Sin ``otros`` en ``details_json`` (run viejo), el deshacer no toca la bandeja."""
    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid)
    ilegible = {**_FILAS[1], "fecha": "sin fecha"}
    _RELEIDO["summary"] = esc.summary([_FILAS[0], ilegible])
    run_id = await _releer(db_session, fid, tid)
    run = await _run(db_session, run_id)
    run.details_json = {k: v for k, v in (run.details_json or {}).items() if k != "otros"}
    await db_session.commit()
    antes = await _otros(db_session, fid)

    r = await client.post(f"/api/v1/ingestion/files/{fid}/reread/undo", headers=auth_headers)

    assert r.status_code == 200, r.text
    assert await _otros(db_session, fid) == antes
    total = await db_session.scalar(select(func.count()).select_from(UnclassifiedRecord))
    assert total == len(antes)


async def test_deshacer_otros_acomoda_las_huellas_de_riesgo_de_columna(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La captura de riesgo que creó la relectura se va con su huella (si no, una
    relectura futura no la vuelve a capturar); la que descartó vuelve con la
    huella que ``_reconcile_column_risk`` le había borrado (si no, se duplica)."""
    import json  # noqa: PLC0415

    from app.persistence.models.memory import OperationFingerprint  # noqa: PLC0415

    tid = sample_tenant.tenant_id
    fid = await _importar(db_session, tid)

    def _captura(idx: int, estado: str) -> UnclassifiedRecord:
        return UnclassifiedRecord(
            tenant_id=tid,
            uploaded_file_id=fid,
            source="reanalysis",
            context_label="Columna riesgosa",
            context_id="table",
            row_data={
                "total": "",
                reread_service._RISK_REF_KEY: json.dumps(
                    {"context_id": "table", "row_index": idx}
                ),
            },
            status=estado,
        )

    def _huella(idx: int) -> str:
        return reread_service._hash_anchor(
            reread_service._risk_row_anchor(tid, fid, "table", idx)
        )

    creada = _captura(0, UNCLASSIFIED_STATUS_PENDING)
    descartada = _captura(1, UNCLASSIFIED_STATUS_DISMISSED)
    db_session.add_all(
        [
            creada,
            descartada,
            OperationFingerprint(
                tenant_id=tid, fingerprint=_huella(0), action_type="IMPORT_ROW"
            ),
        ]
    )
    await db_session.flush()
    creada_id, descartada_id = creada.id, descartada.id

    await reread_service._undo_otros(db_session, tid, fid, [creada_id], [descartada_id])
    await db_session.flush()

    huellas = set(
        (
            await db_session.execute(
                select(OperationFingerprint.fingerprint).where(
                    OperationFingerprint.tenant_id == tid,
                    OperationFingerprint.fingerprint.in_([_huella(0), _huella(1)]),
                )
            )
        ).scalars()
    )
    assert huellas == {_huella(1)}
    assert await _otros(db_session, fid) == {str(descartada_id): UNCLASSIFIED_STATUS_PENDING}
    vuelta = await db_session.get(UnclassifiedRecord, descartada_id)
    assert vuelta is not None and vuelta.status == UNCLASSIFIED_STATUS_PENDING
