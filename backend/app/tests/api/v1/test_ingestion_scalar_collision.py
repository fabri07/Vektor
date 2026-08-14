"""Dos columnas al mismo campo escalar: el confirm rechaza en vez de elegir.

Incidente ASTERIA (2026-07-31): "Precio de compra", "Precio de lista" y "Precio
de venta final" apuntaban las tres a ``sale_price_ars``. El importador
(``_resolve_target_cols``) se quedaba con la PRIMERA del orden del Excel y
descartaba las otras dos sin avisar — el precio que terminaba guardado dependía
de cómo estaba ordenada la planilla.

Elegir un dato de negocio por un detalle de implementación es inventarlo, así que
el confirm ahora corta con 422 y le pide al usuario que decida. El rechazo ocurre
ANTES del lease: una request que va a rebotar nunca lo toma.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_HEADERS = ["Productos", "Precio de compra", "Precio de lista", "Precio de venta final"]


def _catalog_summary() -> dict[str, Any]:
    rows = [
        {
            "Productos": "Vela aromática 200g",
            "Precio de compra": "1200",
            "Precio de lista": "2400",
            "Precio de venta final": "2100",
            "__context__": "sheet:precios y stock",
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        # Como el archivo real: 9 hojas → el importador entra por
        # `_insert_multisheet_data`, que es el único camino que consume los
        # mapeos POR CONTEXTO. Con `inferred_type: "stock"` y sin `multi_sheet`
        # se toma el camino flat y los context_mappings se ignoran.
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "row_count": 1,
        "stock_detectado": rows,
        "mapping_contexts": [
            {
                "context_id": "sheet:precios y stock",
                "label": "precios y stock",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": _HEADERS,
                "fields": None,
                "preview_rows": rows,
                "row_count": 1,
            }
        ],
    }


@pytest_asyncio.fixture
async def catalog_file(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="ASTERIA_home_deco.xlsx",
        s3_key="uploads/test/uuid/asteria.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_catalog_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _mapping(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": "sheet:precios y stock",
        "entity_type": "product",
    }


class TestColisionDeCampoEscalar:
    async def test_tres_columnas_al_mismo_precio_rechaza_con_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """El mapeo exacto que produjo el incidente."""
        body = {
            "column_mappings": [
                _mapping("Productos", "name"),
                _mapping("Precio de compra", "sale_price_ars"),
                _mapping("Precio de lista", "sale_price_ars"),
                _mapping("Precio de venta final", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {"sheet:precios y stock": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        # El mensaje nombra el campo en castellano y las tres columnas en conflicto.
        assert "Precio de venta" in detail
        for col in ("Precio de compra", "Precio de lista", "Precio de venta final"):
            assert col in detail

        # Y NADA se importó: el rechazo es previo a cualquier escritura.
        productos = (await db_session.execute(select(Product))).scalars().all()
        assert productos == []

    async def test_los_tres_precios_a_campos_distintos_no_colisionan(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """La salida que el usuario tiene disponible: un campo para cada uno."""
        body = {
            "column_mappings": [
                _mapping("Productos", "name"),
                _mapping("Precio de compra", "unit_cost_ars"),
                _mapping("Precio de lista", "list_price_ars"),
                _mapping("Precio de venta final", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {"sheet:precios y stock": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text
        prod = (await db_session.execute(select(Product))).scalars().one()
        assert prod.unit_cost_ars is not None
        assert prod.list_price_ars is not None
        assert prod.sale_price_ars is not None
        # Cada número quedó en su campo, ninguno pisó a otro.
        assert len({prod.unit_cost_ars, prod.list_price_ars, prod.sale_price_ars}) == 3

    async def test_columna_dropeada_por_riesgo_no_cuenta_como_colision(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
    ) -> None:
        """La colisión se evalúa sobre el mapeo EFECTIVO, no sobre lo enviado.

        Dos columnas al mismo campo donde el usuario ya decidió dropear una no es
        una colisión: después del drop queda una sola. Bloquear acá sería exigirle
        deshacer una decisión que ya tomó.
        """
        body = {
            "column_mappings": [
                _mapping("Productos", "name"),
                _mapping("Precio de compra", "sale_price_ars"),
                _mapping("Precio de venta final", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {"sheet:precios y stock": True},
            "column_risk_decisions": [
                {
                    "context_id": "sheet:precios y stock",
                    "source_column": "Precio de compra",
                    "target_field": "sale_price_ars",
                    "action": "drop_column",
                }
            ],
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text

    async def test_campo_no_escalar_admite_varias_columnas(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
    ) -> None:
        """`description` no está en SINGLE_VALUE_FIELDS: dos columnas no bloquean.

        Bloquear todo sería tan malo como no bloquear nada — trabaría imports
        legítimos donde varias columnas alimentan un texto.
        """
        body = {
            "column_mappings": [
                _mapping("Productos", "name"),
                _mapping("Precio de compra", "description"),
                _mapping("Precio de lista", "description"),
                _mapping("Precio de venta final", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {"sheet:precios y stock": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{catalog_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text


# Con fecha y monto: los requeridos se validan ANTES que la colisión escalar, así
# que una hoja incompleta rebotaría por el otro motivo y este test no probaría nada.
_HEADERS_COMPRA = [
    "Fecha",
    "Monto",
    "Producto",
    "Cantidad",
    "Bonificación",
    "Descuento",
]


def _compra_summary() -> dict[str, Any]:
    rows = [
        {
            "Fecha": "2026-03-10",
            "Monto": "5000",
            "Producto": "Vela aromática 200g",
            "Cantidad": "10",
            "Bonificación": "150",
            "Descuento": "80",
            "__context__": "sheet:compras",
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "row_count": 1,
        "gastos_detectados": rows,
        "mapping_contexts": [
            {
                "context_id": "sheet:compras",
                "label": "compras",
                "source_kind": "sheet",
                "entity_type": "expense",
                "headers": _HEADERS_COMPRA,
                "fields": None,
                "preview_rows": rows,
                "row_count": 1,
            }
        ],
    }


@pytest_asyncio.fixture
async def compra_file(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="compras.xlsx",
        s3_key="uploads/test/uuid/compras.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="gastos",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_compra_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


class TestLosCostosDeCompraTambienSonEscalares:
    """F-M.7 — `discount`, `taxes` y `shipping_cost_line` son escalares.

    Una planilla real trae «Bonificación» y «Descuento» como columnas separadas y
    las dos son descuentos. Sumarlas sola sería inventar una cuenta que nadie
    pidió; quedarse con la primera del orden del Excel es el incidente ASTERIA
    otra vez, ahora sobre el costo de una compra en vez del precio de un producto.
    """

    async def test_dos_columnas_de_descuento_rechazan_con_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        compra_file: UploadedFile,
    ) -> None:
        body = {
            "column_mappings": [
                {
                    "source_column": c,
                    "target_field": t,
                    "context_id": "sheet:compras",
                    "entity_type": "expense",
                }
                for c, t in [
                    ("Fecha", "expense_date"),
                    ("Monto", "amount"),
                    ("Producto", "product_name"),
                    ("Cantidad", "quantity"),
                    ("Bonificación", "discount"),
                    ("Descuento", "discount"),
                ]
            ],
            "confirmed_fields": {"gastos": True},
            "context_confirmed": {"sheet:compras": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{compra_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "Descuento de la línea" in detail, "el mensaje nombra el campo en castellano"
        for col in ("Bonificación", "Descuento"):
            assert col in detail


class TestUnaDecisionDeCostoQueNoSePuedeHonrar:
    """F-H6.c — se rechaza ANTES del lease, con el motivo en castellano.

    Mismo criterio que la decisión de envíos: declarar un efecto sobre el costo
    que no va a ocurrir no se ignora en silencio, porque el usuario cree haber
    resuelto algo. Y un archivo que va a rebotar no debería tomar el lease.

    El tenant se habilita a mano en la compuerta de rollout (F-H6.d): sin eso el
    confirm rebota antes con «el motor de costos no está habilitado» y este test
    mediría el gate en vez de la validación que le importa.
    """

    @pytest.fixture(autouse=True)
    def _con_motor_de_costos(
        self, monkeypatch: pytest.MonkeyPatch, sample_tenant: Tenant
    ) -> None:
        monkeypatch.setattr(
            get_settings(),
            "PURCHASE_COST_ROLLOUT_TENANT_IDS",
            [str(sample_tenant.tenant_id)],
        )

    async def test_aplicar_ajustes_sin_columna_de_descuento_rechaza(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        compra_file: UploadedFile,
    ) -> None:
        body = {
            "column_mappings": [
                {
                    "source_column": c,
                    "target_field": t,
                    "context_id": "sheet:compras",
                    "entity_type": "expense",
                }
                for c, t in [
                    ("Fecha", "expense_date"),
                    ("Monto", "amount"),
                    ("Producto", "product_name"),
                    ("Cantidad", "quantity"),
                ]
            ],
            "confirmed_fields": {"gastos": True},
            "context_confirmed": {"sheet:compras": True},
            # Declara que el monto es bruto, pero no mapeó ninguna columna de
            # descuento ni de impuestos: el ajuste no tendría de dónde salir.
            "purchase_cost_decisions": [
                {"context_id": "sheet:compras", "base": "monto_sin_ajustes"}
            ],
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{compra_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "compras" in detail, "nombra la hoja"
        assert "Descuento de la línea" in detail, "y el campo en castellano"
        assert "discount" not in detail, "nunca el nombre técnico"

    async def test_un_modo_inventado_rechaza_por_el_schema(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        compra_file: UploadedFile,
    ) -> None:
        """El `Literal` del schema es la primera barrera: un modo que no existe ni
        siquiera llega a la validación de dominio."""
        body = {
            "column_mappings": [
                {
                    "source_column": "Fecha",
                    "target_field": "expense_date",
                    "context_id": "sheet:compras",
                    "entity_type": "expense",
                }
            ],
            "confirmed_fields": {"gastos": True},
            "context_confirmed": {"sheet:compras": True},
            "purchase_cost_decisions": [
                {"context_id": "sheet:compras", "base": "modo_que_no_existe"}
            ],
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{compra_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )
        assert response.status_code == 422


# ── Maestros: la identidad se pisa con el mismo mecanismo que la plata ────────

_PROV_HEADERS = [
    "ID",
    "Razón Social (correcta)",
    "CUIT",
    "Contacto",
    "Teléfono",
    "Email",
    "Variantes de nombre vistas en remitos/WhatsApp",
]


def _proveedores_summary() -> dict[str, Any]:
    rows = [
        {
            "ID": "PROV-01",
            "Razón Social (correcta)": "Distribuidora Norte SRL",
            "CUIT": "30-71234567-8",
            "Contacto": "Marcelo Ibarra",
            "Teléfono": "351-455-1122",
            "Email": "ventas@distribuidoranorte.com",
            "Variantes de nombre vistas en remitos/WhatsApp": "Distrib. Norte, DISTRI NORTE",
            "__context__": "sheet:Proveedores",
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "row_count": 1,
        # El importador de maestros lee las filas de acá (`_ENTITY_TO_SUMMARY_KEY`),
        # no de `preview_rows` del contexto.
        "proveedores_detectados": rows,
        "mapping_contexts": [
            {
                "context_id": "sheet:Proveedores",
                "label": "Proveedores",
                "source_kind": "sheet",
                "entity_type": "supplier",
                "headers": _PROV_HEADERS,
                "fields": None,
                "preview_rows": rows,
                "row_count": 1,
            }
        ],
    }


@pytest_asyncio.fixture
async def proveedores_file(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="Vektor_Test_DistribuidoraLimpieza_3meses.xlsx",
        s3_key="uploads/test/uuid/distribuidora.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=93470,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_proveedores_summary(),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _prov_mapping(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": "sheet:Proveedores",
        "entity_type": "supplier",
    }


class TestColisionEnMaestros:
    """La guarda escalar no cubría `customer` ni `supplier`: los dos entity_type
    tenían el frozenset vacío, así que TODOS sus campos caían en el first-wins
    silencioso de ``_resolve_target_cols``.

    El caso real: una hoja de proveedores con «Contacto» (col 4) y «Teléfono»
    (col 5). Las dos resuelven a `phone` —`contacto` es keyword de `phone` a
    propósito— y como gana la primera del orden del archivo, el teléfono del
    proveedor terminaba siendo el NOMBRE de la persona de contacto. Un monto
    equivocado salta en un total; un teléfono equivocado no salta en ningún lado.
    """

    async def test_contacto_y_telefono_al_mismo_phone_rechaza_con_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        proveedores_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        body = {
            "column_mappings": [
                _prov_mapping("Razón Social (correcta)", "name"),
                _prov_mapping("Contacto", "phone"),
                _prov_mapping("Teléfono", "phone"),
            ],
            "confirmed_fields": {"proveedores": True},
            "context_confirmed": {"sheet:Proveedores": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{proveedores_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "Contacto" in detail
        assert "Teléfono" in detail

        # Nada se importó: el rechazo es previo a cualquier escritura.
        from app.persistence.models.supplier import Supplier  # noqa: PLC0415

        creados = (await db_session.execute(select(Supplier))).scalars().all()
        assert [s for s in creados if not s.is_sentinel] == []

    async def test_dos_columnas_al_nombre_rechaza_con_422(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        proveedores_file: UploadedFile,
    ) -> None:
        """«Variantes de nombre vistas en remitos/WhatsApp» resuelve a `name` por
        su núcleo `nombre`, igual que «Razón Social». En el archivo real esto era
        benigno de casualidad —Razón Social está antes— pero con las columnas en
        otro orden el nombre del proveedor sería una lista de alias."""
        body = {
            "column_mappings": [
                _prov_mapping("Variantes de nombre vistas en remitos/WhatsApp", "name"),
                _prov_mapping("Razón Social (correcta)", "name"),
            ],
            "confirmed_fields": {"proveedores": True},
            "context_confirmed": {"sheet:Proveedores": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{proveedores_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )
        assert response.status_code == 422
        assert "Razón Social (correcta)" in response.json()["detail"]

    async def test_la_salida_es_mandar_la_otra_columna_a_un_campo_propio(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        proveedores_file: UploadedFile,
    ) -> None:
        """La guarda no obliga a tirar la columna: le da destino. Es lo que
        distingue preguntar de bloquear."""
        body = {
            "column_mappings": [
                _prov_mapping("Razón Social (correcta)", "name"),
                _prov_mapping("Teléfono", "phone"),
                _prov_mapping("Contacto", "custom_field:contacto"),
            ],
            "confirmed_fields": {"proveedores": True},
            "context_confirmed": {"sheet:Proveedores": True},
        }
        response = await client.post(
            f"/api/v1/ingestion/files/{proveedores_file.id}/confirm",
            json=body,
            headers=auth_headers,
        )
        assert response.status_code != 422, response.text
