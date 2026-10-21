"""B·9: los dos datos que alimentan las acciones masivas del mapeo de columnas.

- ``empty_in_file``: la columna está vacía en TODAS las filas de su contexto. La
  muestra son 10 filas: una columna vacía al principio y llena después no es
  «vacía», y ofrecer ignorarla tiraría datos.
- ``custom_slug``: el slug que `auto_custom` le daría a una candidata a campo
  propio, desambiguado igual, para que el frontend no espeje `custom_field_slug`.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.column_mapping_service import ColumnMappingService
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant

_CTX = "sheet:catalogo"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


def _summary(*, libro_diario: bool = False) -> dict[str, Any]:
    """12 productos: «Nada» vacía en todas las filas; «Tarde» vacía en las 10
    primeras (la muestra) y llena en las 2 últimas; «Precio» completo."""
    filas = [
        {
            "Producto": f"Vela {i}",
            "Precio": str(1000 + i),
            "Nada": "" if i % 2 else None,
            "Tarde": ("lote B" if i >= 10 else None),
            "__context__": _CTX,
        }
        for i in range(12)
    ]
    ctx: dict[str, Any] = {
        "context_id": _CTX,
        "label": "catalogo",
        "source_kind": "sheet",
        "entity_type": "product",
        "headers": ["Producto", "Precio", "Nada", "Tarde"],
        "fields": None,
        "preview_rows": filas[:10],
        "row_count": len(filas),
    }
    if libro_diario:
        ctx["libro_diario"] = True
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "row_count": len(filas),
        "stock_detectado": filas,
        "mapping_contexts": [ctx],
    }


async def _archivo(db: AsyncSession, tenant: Tenant, summary: dict[str, Any]) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id, uploaded_by=None, original_filename="c.xlsx",
        s3_key="uploads/test/uuid/c.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048, purpose="stock", status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION, parsed_summary_json=summary,
    )
    db.add(record)
    await db.commit()
    return record


async def _sugerencias(
    client: AsyncClient, h: dict[str, str], record: UploadedFile
) -> dict[str, dict[str, Any]]:
    r = await client.get(
        f"/api/v1/ingestion/files/{record.id}/column-mappings",
        params={"context_id": _CTX},
        headers=h,
    )
    assert r.status_code == 200, r.text
    return {s["source_column"]: s for s in r.json()}


async def test_empty_in_file_mide_todas_las_filas_no_la_muestra(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    record = await _archivo(db_session, sample_tenant, _summary())
    por_col = await _sugerencias(client, auth_headers, record)
    assert {c: s["empty_in_file"] for c, s in por_col.items()} == {
        "Producto": False,
        "Precio": False,
        "Nada": True,
        # Vacía en las 10 de la muestra (83 % del archivo) pero con datos al
        # final: no es vacía, y ofrecer ignorarla tiraría esos datos.
        "Tarde": False,
    }
    assert por_col["Tarde"]["sample_values"] == []


async def test_el_libro_diario_nunca_es_vacio(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    record = await _archivo(db_session, sample_tenant, _summary(libro_diario=True))
    por_col = await _sugerencias(client, auth_headers, record)
    assert not any(s["empty_in_file"] for s in por_col.values())


async def test_sin_filas_que_medir_no_es_vacia(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    summary = _summary()
    summary["stock_detectado"] = []
    record = await _archivo(db_session, sample_tenant, summary)
    por_col = await _sugerencias(client, auth_headers, record)
    assert not any(s["empty_in_file"] for s in por_col.values())


def _sug(col: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_column": col,
        "normalized_column": col.lower(),
        "sample_values": ["x"],
        "target_field": None,
        "confidence": 0.0,
        "source": "none",
        "status": "unmapped",
        "options": [],
        "duda": None,
    }
    return {**base, **kw}


async def test_custom_slug_solo_en_candidatas_y_desambiguado(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Misma desambiguación que `auto_custom` (contra los campos propios ya
    presentes en la respuesta y entre sí, en orden) y sus mismas exclusiones
    MENOS la `duda`: el botón es una acción de la persona, y sin las `unmapped`
    con duda no tendría candidatas (`auto_custom` ya archiva las demás)."""
    crudas = [
        _sug("Producto", target_field="name", status="mapped", confidence=0.9),
        _sug("Precio", target_field="custom_field:obs", status="mapped", source="auto_custom"),
        _sug("Obs."),
        _sug("Obs"),
        _sug("Con duda", duda="Esta hoja no tiene dónde guardar eso."),
        _sug("Sin muestra", sample_values=[]),
        _sug("Requerida", status="required_missing"),
        _sug("Ambigua", status="ambiguo", options=["name", "description"]),
        _sug("Excluida", excluida_por_vacia=True),
        _sug("..."),
    ]

    async def _fake(self: Any, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [dict(s) for s in crudas]

    monkeypatch.setattr(ColumnMappingService, "suggest_mappings", _fake)
    summary = _summary()
    summary["mapping_contexts"][0]["headers"] = [s["source_column"] for s in crudas]
    summary["stock_detectado"] = []  # sin medición: B·5f no interviene
    record = await _archivo(db_session, sample_tenant, summary)
    por_col = await _sugerencias(client, auth_headers, record)
    assert {c: s["custom_slug"] for c, s in por_col.items()} == {
        "Producto": None,
        "Precio": None,
        "Obs.": "obs_2",
        "Obs": "obs_3",
        "Con duda": "con_duda",
        "Sin muestra": None,
        "Requerida": None,
        "Ambigua": None,
        "Excluida": None,
        "...": None,
    }
    # El dato es informativo: no cambia el target ni el estado.
    assert (por_col["Obs."]["target_field"], por_col["Obs."]["status"]) == (None, "unmapped")
