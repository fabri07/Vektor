"""B·5f: una columna con ≥ 40 % de celdas vacías no se carga, y se avisa.

Decisión del dueño (2026-10-20). Se mide sobre TODAS las filas de la hoja.

- Mapeada sola (sugerencia) a un destino informativo → no se carga: no llega al
  importador, el confirm lo avisa y el comprobante la marca «excluido» por regla
  del sistema. Las sugerencias ya no la mapean.
- Elegida a mano por el usuario → se respeta y se avisa.
- Con un destino que mueve números o identidad (precio, monto, nombre…) → se
  carga igual y se avisa: sacarla cambiaría las filas que sí la traen.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.column_risk import (
    UMBRAL_COLUMNA_VACIA,
    columnas_vacias,
    destino_excluible_por_vacio,
)
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_CTX = "sheet:catalogo"
_HEADERS = ["Producto", "Precio", "Descripcion", "Lista"]


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


def _filas() -> list[dict[str, Any]]:
    """5 productos: «Descripcion» vacía en 3 (60 %), «Lista» vacía en 2 (40 %),
    precio completo. Las vacías están al FINAL: una muestra de las primeras filas
    no las ve."""
    return [
        {
            "Producto": f"Vela {i}",
            "Precio": str(1000 + i),
            "Descripcion": ("Aroma vainilla" if i < 2 else None),
            "Lista": (str(2000 + i) if i < 3 else ""),
            "__context__": _CTX,
        }
        for i in range(5)
    ]


def _summary() -> dict[str, Any]:
    filas = _filas()
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "row_count": len(filas),
        "stock_detectado": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "catalogo",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": _HEADERS,
                "fields": None,
                "preview_rows": filas[:2],
                "row_count": len(filas),
            }
        ],
    }


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="catalogo.xlsx",
        s3_key="uploads/test/uuid/catalogo.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _m(source: str, target: str, *, elegida: bool = False) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CTX,
        "entity_type": "product",
        "user_selected": elegida,
    }


async def _confirmar(
    client: AsyncClient, h: dict[str, str], archivo: UploadedFile, *, elegida: bool
) -> dict[str, Any]:
    r = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": [
                _m("Producto", "name"),
                _m("Precio", "sale_price_ars"),
                _m("Descripcion", "description", elegida=elegida),
                _m("Lista", "list_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CTX: True},
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


# ── Unidad ────────────────────────────────────────────────────────────────────


def test_se_mide_sobre_todas_las_filas_y_el_umbral_es_40_inclusive() -> None:
    assert UMBRAL_COLUMNA_VACIA == 0.40
    vacias = columnas_vacias(_summary(), _CTX)
    assert vacias == {"Descripcion": 0.6, "Lista": 0.4}


@pytest.mark.parametrize(
    ("destino", "excluible"),
    [
        ("description", True),
        ("notes", True),
        ("customer_phone", True),
        ("custom_field:color", True),
        ("customer:email", True),
        ("amount", False),
        ("sale_price_ars", False),
        ("unit_cost_ars", False),
        ("name", False),
        ("sku", False),
        ("payment_method", False),
        ("transaction_date", False),
    ],
)
def test_solo_se_excluyen_solos_los_destinos_informativos(destino: str, excluible: bool) -> None:
    assert destino_excluible_por_vacio(destino) is excluible


# ── Confirm ───────────────────────────────────────────────────────────────────


async def test_una_columna_informativa_vacia_mapeada_sola_no_se_carga(
    client: AsyncClient,
    auth_headers: dict[str, str],
    archivo: UploadedFile,
    db_session: AsyncSession,
) -> None:
    body = await _confirmar(client, auth_headers, archivo, elegida=False)
    productos = (await db_session.execute(select(Product).order_by(Product.name))).scalars().all()
    assert len(productos) == 5
    # Ni la descripción (60 %) ni el precio de lista (40 %) se cargaron; el
    # precio de venta, completo, sí.
    assert {p.description for p in productos} == {None}
    assert {p.list_price_ars for p in productos} == {None}
    assert sorted(str(p.sale_price_ars) for p in productos)[0] == "1000.00"
    avisos = body["warnings"]
    assert "La columna «Descripcion» no se carga: está 60% vacía." in avisos
    assert "La columna «Lista» no se carga: está 40% vacía." in avisos

    r = await client.get(f"/api/v1/ingestion/files/{archivo.id}/receipt", headers=auth_headers)
    assert r.status_code == 200, r.text
    filas = {c["source_column"]: c for c in r.json()["last_applied"]["receipt"]["columns"]}
    assert filas["Descripcion"]["result"] == "excluido"
    assert filas["Descripcion"]["reason_kind"] == "system_rule"
    assert "60% vacía" in filas["Descripcion"]["reason"]
    assert filas["Precio"]["result"] != "excluido"


async def test_elegida_a_mano_se_respeta_y_se_avisa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    archivo: UploadedFile,
    db_session: AsyncSession,
) -> None:
    body = await _confirmar(client, auth_headers, archivo, elegida=True)
    productos = (await db_session.execute(select(Product))).scalars().all()
    assert sorted(p.description or "" for p in productos) == [
        "", "", "", "Aroma vainilla", "Aroma vainilla"
    ]
    assert (
        "La columna «Descripcion» está 60% vacía: se carga porque la elegiste vos."
        in body["warnings"]
    )


async def test_una_columna_de_la_operacion_vacia_se_carga_igual_y_se_avisa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El costo vacío en 3 de 5: sacarlo dejaría sin costo a las 2 que lo traen."""
    summary = _summary()
    for i, fila in enumerate(summary["stock_detectado"]):
        fila["Costo"] = "400" if i < 2 else None
    summary["mapping_contexts"][0]["headers"] = [*_HEADERS, "Costo"]
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id, uploaded_by=None, original_filename="c2.xlsx",
        s3_key="uploads/test/uuid/c2.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048, purpose="stock", status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION, parsed_summary_json=summary,
    )
    db_session.add(record)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": [
                _m("Producto", "name"), _m("Precio", "sale_price_ars"),
                _m("Costo", "unit_cost_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CTX: True},
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    costos = sorted(
        str(p.unit_cost_ars) for p in (await db_session.execute(select(Product))).scalars()
        if p.unit_cost_ars is not None
    )
    assert costos == ["400.00", "400.00"]
    assert any("«Costo» está 60% vacía: se carga igual" in w for w in r.json()["warnings"])


# ── Sugerencias ───────────────────────────────────────────────────────────────


async def test_la_sugerencia_no_mapea_sola_una_columna_informativa_vacia(
    client: AsyncClient,
    auth_headers: dict[str, str],
    archivo: UploadedFile,
) -> None:
    r = await client.get(
        f"/api/v1/ingestion/files/{archivo.id}/column-mappings",
        params={"context_id": _CTX},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    por_col = {s["source_column"]: s for s in r.json()}
    desc = por_col["Descripcion"]
    assert (
        desc["target_field"], desc["status"], desc["excluida_por_vacia"], desc["vacia_pct"]
    ) == (None, "unmapped", True, 60)
    assert desc["duda"].startswith("Está 60% vacía: no se carga.")
    precio = por_col["Precio"]
    assert (precio["vacia_pct"], precio["excluida_por_vacia"]) == (None, False)
