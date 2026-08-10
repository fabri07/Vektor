"""F-H6.d — el preview del reparto agrupa IGUAL que el import.

Elegir «repartir el envío por subtotal» sin ver el resultado es aceptar a ciegas
un cambio en el costo de cada producto. Este endpoint muestra qué líneas quedaron
juntas y cuánto le tocó a cada una.

Lo que estos tests vigilan es la garantía que reclama por escrito el docstring de
`identidad_de_comprobante`: la agrupación, la validación previa al lease y el
preview tienen que responder lo mismo. Si divergen, «la pantalla ofrece repartir
un costo entre líneas que el importador después no va a agrupar, y el usuario ve
un reparto que no ocurrió».

Por eso el test central no compara contra números escritos a mano: pasa el MISMO
archivo con el MISMO mapeo por el endpoint y por `insert_confirmed_data`, y exige
que el costo unitario que anuncia el preview sea exactamente el que queda
persistido. Un test con números fijos pasaría aunque las dos rutas se movieran
juntas hacia el mismo lugar equivocado; éste no puede.

El archivo de prueba tiene DOS proveedores compartiendo el número de comprobante
a propósito: es el caso que distingue agrupar por `(proveedor, comprobante)` de
agrupar sólo por el número. Con el criterio equivocado los dos remitos se funden
en uno, las cifras de envío dejan de coincidir y no se reparte nada.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.config.settings import get_settings
from app.domain.purchase_cost_decision import PurchaseCostDecision
from app.persistence.models.file import (
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    PROCESSING_STATUS_PENDING,
    UploadedFile,
)
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_CTX = "sheet:Compras"

_HEADERS = [
    "fecha",
    "articulo",
    "cantidad",
    "total",
    "envio",
    "comprobante",
    "proveedor",
]

_MAPEO = {
    "fecha": "expense_date",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "envio": "shipping_cost",
    "comprobante": "invoice_number",
    "proveedor": "supplier_name",
}

#: Dos proveedores con el MISMO número de comprobante. Sur reparte 300 entre dos
#: líneas de 1000 y 3000 (→ 75 y 225); Norte reparte 100 sobre su única línea.
_FILAS: list[dict[str, Any]] = [
    {
        "fecha": "2024-03-05",
        "articulo": "Vela aromatica 200g",
        "cantidad": "10",
        "total": "1000",
        "envio": "300",
        "comprobante": "A-0001",
        "proveedor": "Distribuidora Sur",
    },
    {
        "fecha": "2024-03-05",
        "articulo": "Portarretrato madera",
        "cantidad": "10",
        "total": "3000",
        "envio": "300",
        "comprobante": "A-0001",
        "proveedor": "Distribuidora Sur",
    },
    {
        "fecha": "2024-03-06",
        "articulo": "Difusor bambu",
        "cantidad": "10",
        "total": "1000",
        "envio": "100",
        "comprobante": "A-0001",
        "proveedor": "Norte SRL",
    },
]

_DECISION_REPARTIR = {
    "context_id": _CTX,
    "base": "monto_incluye",
    "shared_shipping": "por_subtotal",
    "line_shipping": "gasto_aparte",
}


def _summary(filas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Compras",
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": _HEADERS,
                "fields": None,
                "preview_rows": [],
                "row_count": len(filas),
            }
        ],
        "gastos_detectados": [{**f, "__context__": _CTX} for f in filas],
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


def _column_mappings(mapeo: dict[str, str] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "source_column": src,
            "target_field": target,
            "context_id": _CTX,
            "entity_type": "expense",
        }
        for src, target in (mapeo or _MAPEO).items()
    ]


async def _crear_archivo(
    db: AsyncSession,
    tenant: Tenant,
    summary: dict[str, Any],
    *,
    processing_status: str = PROCESSING_STATUS_NEEDS_CONFIRMATION,
) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="compras.xlsx",
        s3_key="uploads/test/uuid/compras.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="gastos",
        status="uploaded",
        processing_status=processing_status,
        parsed_summary_json=summary,
    )
    db.add(record)
    await db.commit()
    return record


@pytest.fixture(autouse=True)
def tenant_habilitado(
    monkeypatch: pytest.MonkeyPatch, sample_tenant: Tenant
) -> None:
    """El motor de costos de compra sale con la allowlist VACÍA (nadie
    habilitado). Estos tests miran el motor prendido, así que habilitan su
    tenant explícitamente — igual que se lo va a encender en producción, de a uno.
    """
    monkeypatch.setattr(
        get_settings(),
        "PURCHASE_COST_ROLLOUT_TENANT_IDS",
        [str(sample_tenant.tenant_id)],
    )


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    return await _crear_archivo(db_session, sample_tenant, _summary(_FILAS))


async def _pedir_grupos(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    archivo: UploadedFile,
    *,
    decisiones: list[dict[str, Any]] | None = None,
    mapeo: dict[str, str] | None = None,
    envios: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/purchase-groups",
        json={
            "column_mappings": _column_mappings(mapeo),
            "purchase_cost_decisions": decisiones or [],
            "shipping_decisions": envios or [],
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


class TestElPreviewNoDivergeDelImport:
    async def test_el_costo_unitario_que_anuncia_es_el_que_queda_guardado(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El test que importa. Mismo archivo, mismo mapeo, misma decisión: lo que
        la pantalla promete tiene que ser lo que el importador escribe."""
        body = await _pedir_grupos(
            client, auth_headers, archivo, decisiones=[_DECISION_REPARTIR]
        )
        hoja = body["sheets"][0]
        anunciado = {
            linea["producto"]: linea["costo_unitario_final"]
            for grupo in hoja["grupos"]
            for linea in grupo["lineas"]
        }

        await insert_confirmed_data(
            db_session,
            sample_tenant.tenant_id,
            _summary(_FILAS),
            {"gastos": True},
            context_mappings={_CTX: _MAPEO},
            context_confirmed={_CTX: True},
            purchase_cost_decisions={
                _CTX: PurchaseCostDecision(
                    context_id=_CTX,
                    base="monto_incluye",
                    shared_shipping="por_subtotal",
                    line_shipping="gasto_aparte",
                )
            },
        )
        await db_session.flush()
        productos = (
            (
                await db_session.execute(
                    select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        persistido = {
            p.name: str(Decimal(str(p.unit_cost_ars)).quantize(Decimal("0.01")))
            for p in productos
        }

        assert persistido, "el import no creó ningún producto: el test no prueba nada"
        assert anunciado == persistido

    async def test_agrupa_por_proveedor_y_comprobante_no_solo_por_numero(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """Dos proveedores con el mismo `A-0001` son DOS compras.

        Fundirlas por el número haría que sus cifras de envío (300 y 100) queden
        en el mismo grupo, y ahí no se reparte nada: dos cifras distintas en un
        comprobante pueden ser un flete y un seguro.
        """
        body = await _pedir_grupos(
            client, auth_headers, archivo, decisiones=[_DECISION_REPARTIR]
        )
        hoja = body["sheets"][0]

        assert hoja["grupos_total"] == 2
        por_proveedor = {g["proveedor"]: g for g in hoja["grupos"]}
        # Con la grafía del ARCHIVO, no con la clave normalizada que se usa para
        # agrupar: la pantalla muestra «Distribuidora Sur», no «distribuidora sur».
        assert set(por_proveedor) == {"Distribuidora Sur", "Norte SRL"}

        sur = por_proveedor["Distribuidora Sur"]
        assert sur["comprobante"] == "A-0001"
        assert sur["distribuible"] is True
        # La cifra repetida en las dos filas llega COLAPSADA: un envío, no dos.
        assert sur["envio_compartido"] == "300.00"
        assert sur["repartido"] == "300.00"
        assert sur["sin_repartir"] == "0.00"
        asignado = {linea["producto"]: linea["envio_asignado"] for linea in sur["lineas"]}
        assert asignado == {
            "Vela aromatica 200g": "75.00",
            "Portarretrato madera": "225.00",
        }

    async def test_el_default_muestra_lo_que_no_se_reparte(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        archivo: UploadedFile,
    ) -> None:
        """Sin decisión, Véktor no reparte — y el preview lo DICE en números.

        Es el estado en el que llega la pantalla: la persona tiene que poder ver
        que hay 400 de envío sin tocar ningún costo antes de decidir moverlos.
        """
        body = await _pedir_grupos(client, auth_headers, archivo)
        grupos = body["sheets"][0]["grupos"]

        assert all(g["repartido"] == "0.00" for g in grupos)
        assert {g["sin_repartir"] for g in grupos} == {"300.00", "100.00"}
        # Distribuible sigue siendo True: el archivo PERMITE repartir, lo que
        # falta es que el usuario lo pida. Son dos cosas distintas y confundirlas
        # dejaría la opción escondida justo donde sí se puede ofrecer.
        assert all(g["distribuible"] for g in grupos)


class TestCuandoElArchivoNoPermiteRepartir:
    async def test_sin_columna_de_comprobante_lo_dice_en_castellano(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        mapeo = {k: v for k, v in _MAPEO.items() if k != "comprobante"}
        archivo = await _crear_archivo(db_session, sample_tenant, _summary(_FILAS))
        body = await _pedir_grupos(
            client,
            auth_headers,
            archivo,
            decisiones=[_DECISION_REPARTIR],
            mapeo=mapeo,
        )
        hoja = body["sheets"][0]

        assert hoja["puede_distribuir"] is False
        assert hoja["motivo"]
        # Explica el problema y la salida, sin nombres técnicos.
        assert "comprobante" in hoja["motivo"]
        assert "invoice_number" not in hoja["motivo"]
        # Las filas siguen contadas: es el dato que decide si tiene sentido
        # ofrecer «toda la hoja es una sola compra».
        assert hoja["filas_sin_comprobante"] == len(_FILAS)

    async def test_declarar_que_la_hoja_es_una_compra_habilita_el_reparto(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Control del test de arriba: sin esto, un `puede_distribuir=False`
        constante lo daría por bueno igual.

        La decisión que habilita es la MISMA que gobierna el cobro del flete
        (`una_por_hoja`), no una segunda llave propia de esta pantalla.
        """
        mapeo = {k: v for k, v in _MAPEO.items() if k != "comprobante"}
        # Una sola cifra de envío: con la hoja declarada como un comprobante, dos
        # cifras distintas seguirían sin repartirse (y con razón).
        filas = [{**f, "envio": "300"} for f in _FILAS]
        archivo = await _crear_archivo(db_session, sample_tenant, _summary(filas))

        body = await _pedir_grupos(
            client,
            auth_headers,
            archivo,
            decisiones=[_DECISION_REPARTIR],
            mapeo=mapeo,
            envios=[{"context_id": _CTX, "action": "una_por_hoja"}],
        )
        hoja = body["sheets"][0]

        assert hoja["puede_distribuir"] is True
        assert hoja["motivo"] is None
        assert hoja["grupos"][0]["repartido"] == "300.00"

    async def test_una_hoja_sin_columnas_de_costo_no_inventa_grupos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        mapeo = {
            "fecha": "expense_date",
            "articulo": "product_name",
            "cantidad": "quantity",
            "total": "amount",
        }
        archivo = await _crear_archivo(db_session, sample_tenant, _summary(_FILAS))
        body = await _pedir_grupos(client, auth_headers, archivo, mapeo=mapeo)
        hoja = body["sheets"][0]

        assert hoja["grupos"] == []
        assert hoja["grupos_total"] == 0
        assert hoja["puede_distribuir"] is False
        assert hoja["motivo"]

    async def test_una_hoja_de_ventas_no_aparece(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El reparto del costo compartido es un problema de compras: una hoja de
        ventas no tiene comprobante de proveedor que repartir."""
        summary = _summary(_FILAS)
        summary["mapping_contexts"][0]["entity_type"] = "sale"
        summary["ventas_detectadas"] = summary.pop("gastos_detectados")
        summary["gastos_detectados"] = []
        archivo = await _crear_archivo(db_session, sample_tenant, summary)

        body = await _pedir_grupos(client, auth_headers, archivo)
        assert body["sheets"] == []


class TestGuardsIgualQueSusHermanos:
    async def test_archivo_inexistente_404(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        response = await client.post(
            "/api/v1/ingestion/files/00000000-0000-0000-0000-000000000000/purchase-groups",
            json={"column_mappings": []},
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_archivo_todavia_procesando_409(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        archivo = await _crear_archivo(
            db_session,
            sample_tenant,
            _summary(_FILAS),
            processing_status=PROCESSING_STATUS_PENDING,
        )
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/purchase-groups",
            json={"column_mappings": []},
            headers=auth_headers,
        )
        assert response.status_code == 409

    async def test_requiere_autenticacion(
        self, client: AsyncClient, archivo: UploadedFile
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{archivo.id}/purchase-groups",
            json={"column_mappings": []},
        )
        assert response.status_code in (401, 403)

