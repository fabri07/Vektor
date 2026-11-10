"""Hoja «Clientes» con filas sin DNI/CUIT/email/teléfono (pruebas del dueño, 2026-11-10).

«Caro G.» y «Reventa Belén» no generaron clientes y nadie dijo por qué: sin clave
fuerte la fila cae en ``needs_review`` por diseño («el nombre nunca identifica
solo») y se salteaba EN SILENCIO. Ahora va a «Otros» con el motivo, sin destino
sugerido (así «Importar todo lo sugerido» no puede crearla por nombre).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.file_parsing import analyze_headers
from app.persistence.models.customer import Customer
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import UnclassifiedRecord


def _resumen() -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "clientes",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Clientes",
                "label": "Clientes",
                "entity_type": "customer",
                "source_kind": "sheet",
                "headers": ["Nombre", "Documento"],
                "fields": None,
                "preview_rows": [],
                "row_count": 3,
            },
        ],
        "clientes_detectados": [
            {"Nombre": "Juan Perez", "Documento": "30123456", "__context__": "sheet:Clientes"},
            {"Nombre": "Caro G.", "Documento": "", "__context__": "sheet:Clientes"},
            {"Nombre": "Reventa Belén", "Documento": "", "__context__": "sheet:Clientes"},
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [],
        "proveedores_detectados": [],
    }


async def test_fila_sin_identificacion_va_a_otros_y_no_se_crea_por_nombre(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _resumen(),
        {"clientes": True},
        context_mappings={"sheet:Clientes": {"Nombre": "name", "Documento": "dni"}},
        context_confirmed={"sheet:Clientes": True},
    )
    await db_session.flush()

    # Sólo el que trae documento se crea.
    nombres = [c.name for c in (await db_session.execute(select(Customer))).scalars()]
    assert "Juan Perez" in nombres
    assert "Caro G." not in nombres and "Reventa Belén" not in nombres

    # Los otros dos NO se pierden: quedan en «Otros» con el motivo.
    assert counts["clientes_needs_review"] == 2
    assert counts["clientes_a_otros"] == 2
    pendientes = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert sorted(p.row_data["Nombre"] for p in pendientes) == ["Caro G.", "Reventa Belén"]
    for p in pendientes:
        assert "sin dato de identificación" in (p.context_label or "")
        assert p.suggested_entity is None  # «Importar todo lo sugerido» no puede crearla


def test_maestro_de_contactos_no_se_clasifica_como_productos() -> None:
    """«Nombre + teléfono/email» sin señal de cliente ni de proveedor era `stock`
    (productos); ahora es ambiguo y el nombre de la hoja desempata."""
    for headers in (["Nombre", "Teléfono"], ["Nombre", "Email", "Teléfono"]):
        assert analyze_headers(headers)["inferred_type"] == "general", headers
    # Un catálogo con las columnas propias de un catálogo sigue siendo stock.
    assert analyze_headers(["Producto", "Precio", "Stock"])["inferred_type"] == "stock"
