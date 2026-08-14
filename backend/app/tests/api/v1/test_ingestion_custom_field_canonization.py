"""F-A.3 — la clave de un campo propio se canoniza UNA vez, al entrar al confirm.

``parse_target`` es un parser: de la clave sólo hace ``.strip()``. Río abajo hay
siete consumidores que la vuelven a parsear por su cuenta y dos que comparan el
string crudo sin parsearlo (``_trae_maestros`` y el aprendizaje de alias). Sin
una pasada única, la misma columna se valida con una forma y se persiste con
otra.

Los tests van por HTTP a propósito: el defecto que cierran no está en ninguna
función suelta, está en el ORDEN de la tubería del confirm.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.field_definitions import TenantCustomFieldDefinition
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_CONTEXT = "sheet:catalogo"
_HEADERS = ["Productos", "Precio de venta final", "Año Fiscal", "Obs."]


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, el trigger de score post-confirm paga reintentos de kombu."""


def _summary() -> dict[str, Any]:
    rows = [
        {
            "Productos": "Vela aromática 200g",
            "Precio de venta final": "2100",
            "Año Fiscal": "2026",
            "Obs.": "fragil",
            "__context__": _CONTEXT,
        }
    ]
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "row_count": 1,
        "stock_detectado": rows,
        "mapping_contexts": [
            {
                "context_id": _CONTEXT,
                "label": "catalogo",
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


def _mapping(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": _CONTEXT,
        "entity_type": "product",
    }


_BASE = [
    _mapping("Productos", "name"),
    _mapping("Precio de venta final", "sale_price_ars"),
]


async def _confirm(
    client: AsyncClient, file_id: Any, auth_headers: dict[str, Any], extra: list[dict[str, Any]]
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{file_id}/confirm",
        json={
            "column_mappings": [*_BASE, *extra],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {_CONTEXT: True},
        },
        headers=auth_headers,
    )


class TestLaClavePersistidaEsLaCanonica:
    async def test_un_encabezado_crudo_no_se_guarda_tal_cual(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """`custom_field:Año Fiscal` se persiste como `ano_fiscal`.

        Antes la clave viajaba con acentos, mayúsculas y espacios hasta
        `field_key`, una columna que `POST /fields` valida con
        `^[a-z][a-z0-9_]*$`: la ingesta escribía lo que su propia API rechaza.
        """
        response = await _confirm(
            client,
            catalog_file.id,
            auth_headers,
            [_mapping("Año Fiscal", "custom_field:Año Fiscal")],
        )
        assert response.status_code == 200, response.text

        definiciones = (
            (await db_session.execute(select(TenantCustomFieldDefinition))).scalars().all()
        )
        claves = {d.field_key for d in definiciones}
        assert "ano_fiscal" in claves
        assert "Año Fiscal" not in claves

    async def test_la_fila_importada_usa_la_misma_clave(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """El valor tiene que quedar bajo la clave canónica, no la cruda.

        Es la mitad que un test de la definición no ve: si el importador escribe
        `custom_fields["Año Fiscal"]` mientras la definición dice `ano_fiscal`,
        el dato existe y la pantalla de campos propios no lo encuentra nunca.
        """
        response = await _confirm(
            client,
            catalog_file.id,
            auth_headers,
            [_mapping("Año Fiscal", "custom_field:Año Fiscal")],
        )
        assert response.status_code == 200, response.text

        producto = (await db_session.execute(select(Product))).scalars().one()
        assert (producto.custom_fields or {}).get("ano_fiscal") == "2026"
        assert "Año Fiscal" not in (producto.custom_fields or {})


class TestElOrdenImporta:
    async def test_dos_formas_del_mismo_nombre_colisionan(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """`custom_field:Obs.` y `custom_field:obs` son la MISMA clave.

        Este es el test que prueba el orden: la canonización tiene que correr
        ANTES de `_colliding_custom_fields`. Sin eso los dos targets se ven
        distintos, pasan la validación y recién colapsan al escribir — con una
        de las dos columnas perdida en silencio, que es el last-wins que este
        pipeline existe para evitar.
        """
        response = await _confirm(
            client,
            catalog_file.id,
            auth_headers,
            [
                _mapping("Año Fiscal", "custom_field:obs"),
                _mapping("Obs.", "custom_field:Obs."),
            ],
        )

        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "Año Fiscal" in detail
        assert "Obs." in detail
        assert (await db_session.execute(select(Product))).scalars().all() == []


class TestNombreSinNadaUsable:
    async def test_una_clave_sin_letras_ni_numeros_rechaza_con_la_columna_nombrada(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Antes esto creaba una definición con `field_key` vacío.

        Y como todas las columnas así colapsaban en la misma clave vacía, la
        segunda pisaba a la primera sin que nada lo dijera.
        """
        response = await _confirm(
            client,
            catalog_file.id,
            auth_headers,
            [_mapping("Obs.", "custom_field:...")],
        )

        assert response.status_code == 422, response.text
        assert "Obs." in response.json()["detail"]

        definiciones = (
            (await db_session.execute(select(TenantCustomFieldDefinition))).scalars().all()
        )
        assert [d for d in definiciones if not d.field_key] == []
        assert (await db_session.execute(select(Product))).scalars().all() == []


class TestElLabelLlegaHastaLaDefinicion:
    """F-A — el recorrido del label termina en `ensure_custom_field_exists`.

    Es el tramo que el plan rector no cubría: `target_label` existía en la
    sugerencia y el confirm igual derivaba la etiqueta de `source_column`. Los
    dos coinciden sólo mientras nadie renombre la columna en pantalla.
    """

    async def test_se_persiste_el_label_que_mando_la_pantalla(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        mapping = _mapping("Obs.", "custom_field:notas_del_cliente")
        mapping["target_label"] = "Notas del cliente"

        response = await _confirm(client, catalog_file.id, auth_headers, [mapping])
        assert response.status_code == 200, response.text

        definicion = (
            (await db_session.execute(select(TenantCustomFieldDefinition))).scalars().one()
        )
        assert definicion.field_key == "notas_del_cliente"
        # Y NO "Obs.", que es como se llama la columna en el archivo.
        assert definicion.override_label == "Notas del cliente"

    async def test_sin_label_cae_al_nombre_de_la_columna(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        catalog_file: UploadedFile,
        db_session: AsyncSession,
    ) -> None:
        """Un cliente viejo que no manda `target_label` sigue funcionando.

        El campo es opcional a propósito: agregarlo no puede romper a quien ya
        está confirmando archivos.
        """
        response = await _confirm(
            client, catalog_file.id, auth_headers, [_mapping("Obs.", "custom_field:obs")]
        )
        assert response.status_code == 200, response.text

        definicion = (
            (await db_session.execute(select(TenantCustomFieldDefinition))).scalars().one()
        )
        assert definicion.override_label == "Obs."
