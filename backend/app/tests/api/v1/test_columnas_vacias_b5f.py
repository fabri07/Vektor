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
    ("destino", "entidad", "excluible"),
    [
        ("description", "product", True),
        ("notes", "sale", True),
        ("category", "product", True),
        ("custom_field:color", "sale", True),
        ("address", "customer", True),
        # Revisión: identidad o números, nunca solos.
        ("category", "expense", False),
        ("customer_phone", "sale", False),
        ("customer_email", "sale", False),
        ("customer:email", "sale", False),
        ("is_recurring", "expense", False),
        ("amount", "sale", False),
        ("sale_price_ars", "product", False),
        ("unit_cost_ars", "product", False),
        ("name", "product", False),
        ("sku", "product", False),
        ("payment_method", "sale", False),
        ("transaction_date", "sale", False),
    ],
)
def test_solo_se_excluyen_solos_los_destinos_informativos(
    destino: str, entidad: str, excluible: bool
) -> None:
    assert destino_excluible_por_vacio(destino, entidad) is excluible


def test_la_relectura_vuelve_a_dejar_afuera_lo_excluido() -> None:
    from app.application.services.reread_service import _con_vacias_excluidas

    assert _con_vacias_excluidas(None, {_CTX: ["Rubro"]}) == {_CTX: {"Rubro": "ignore"}}
    # Lo que el usuario mapeó en el borrador gana.
    assert _con_vacias_excluidas({_CTX: {"Rubro": "category"}}, {_CTX: ["Rubro"]}) == {
        _CTX: {"Rubro": "category"}
    }
    assert _con_vacias_excluidas({_CTX: {"A": "name"}}, None) == {_CTX: {"A": "name"}}


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
    ) == ("ignore", "mapped", True, 60)
    assert desc["duda"].startswith("Está 60% vacía: no se carga.")
    precio = por_col["Precio"]
    assert (precio["vacia_pct"], precio["excluida_por_vacia"]) == (None, False)


# ── Revisión de B·5f ──────────────────────────────────────────────────────────


def _summary_con_rubro() -> dict[str, Any]:
    """«Rubro» vacío en 3 de 5: la categoría tiene heurística por nombre de
    columna en el importador, así que sacarla del mapeo no alcanza."""
    summary = _summary()
    for i, fila in enumerate(summary["stock_detectado"]):
        fila["Rubro"] = "Velas" if i < 2 else None
    summary["mapping_contexts"][0]["headers"] = [*_HEADERS, "Rubro"]
    return summary


async def _archivo_con(db: AsyncSession, tenant: Tenant, summary: dict[str, Any]) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id, uploaded_by=None, original_filename="r.xlsx",
        s3_key="uploads/test/uuid/r.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048, purpose="stock", status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION, parsed_summary_json=summary,
    )
    db.add(record)
    await db.commit()
    return record


@pytest.mark.parametrize("como_llega", ["category", "ignore"])
async def test_la_categoria_vacia_no_se_carga_por_la_heuristica(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    como_llega: str,
) -> None:
    """Revisión: con el destino sólo quitado del mapeo, la heurística del
    importador leía «Rubro» y la cargaba, mientras el comprobante decía
    «excluido». Llegue como `category` sugerido o como `ignore` (lo que propone
    la sugerencia), ningún producto queda con categoría."""
    record = await _archivo_con(db_session, sample_tenant, _summary_con_rubro())
    mapeos = [_m("Producto", "name"), _m("Precio", "sale_price_ars"), _m("Rubro", como_llega)]
    r = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": mapeos,
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CTX: True},
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    productos = (await db_session.execute(select(Product))).scalars().all()
    assert len(productos) == 5
    assert {p.category for p in productos} == {None}
    assert not any((p.custom_fields or {}).get("category_label") for p in productos)
    assert "La columna «Rubro» no se carga: está 60% vacía." in r.json()["warnings"]


async def test_la_sugerencia_propone_ignorar_y_conserva_las_ambiguas(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    record = await _archivo_con(db_session, sample_tenant, _summary_con_rubro())
    r = await client.get(
        f"/api/v1/ingestion/files/{record.id}/column-mappings",
        params={"context_id": _CTX},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    rubro = next(s for s in r.json() if s["source_column"] == "Rubro")
    assert (rubro["target_field"], rubro["excluida_por_vacia"], rubro["vacia_pct"]) == (
        "ignore", True, 60
    )


async def test_el_libro_diario_no_se_mide(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Sus columnas son sintéticas del parser (la forma de pago sólo en los
    fiados): no hay columna del usuario que avisar."""
    summary = _summary()
    summary["mapping_contexts"][0]["libro_diario"] = True
    record = await _archivo_con(db_session, sample_tenant, summary)
    r = await client.get(
        f"/api/v1/ingestion/files/{record.id}/column-mappings",
        params={"context_id": _CTX},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert {s["source_column"]: s["vacia_pct"] for s in r.json()}["Descripcion"] is None
