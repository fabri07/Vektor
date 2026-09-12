"""E6b — el camino histórico sin `mapping_contexts` no importa operaciones a ciegas.

El límite que E6b declaró y no cerró: un summary sin `mapping_contexts` que entra
por el camino multihoja persiste ventas y gastos **sin calcular ni consultar
identidad**, así que una fila que entre por ahí puede duplicar una operación que
otro archivo ya cargó con identidad. Nadie puede saber que es la misma.

La salida NO es adivinar la identidad en ese camino —sin mapeo de columnas los
lectores resuelven por keyword, y una clave de identidad adivinada desde un
encabezado es exactamente lo que `domain/operation_identity` se niega a producir:
una clave falsa hace que Véktor descarte plata real creyendo que ya la tenía—. La
salida es **no importar así** y ofrecer la relectura, que regenera el summary con
contextos usando el parser de hoy.

Medido antes de escribir esto: ningún camino de parseo actual produce un summary
con operaciones y sin contextos (los únicos sin contextos son los de archivos
vacíos). O sea que esto sólo alcanza a summaries persistidos ANTES del mapeo
universal — por eso el rechazo tiene que nombrar la relectura y no "volvé a
subirlo": el archivo del usuario está perfecto, lo viejo es su interpretación.

El bloqueo es ACOTADO a propósito, y cada exclusión tiene su prueba acá:
archivos vacíos, documentos que rutean a «Otros», imports que sólo traen
productos, y la tabla suelta —que no pasa por esta rama y sí calcula identidad—.
"""

from __future__ import annotations

import unittest.mock
from typing import Any

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _archivo(
    tenant_id: Any, summary: dict[str, Any], *, nombre: str = "historico.xlsx"
) -> UploadedFile:
    return UploadedFile(
        tenant_id=tenant_id,
        uploaded_by=None,
        original_filename=nombre,
        s3_key=f"uploads/test/uuid/{nombre}",
        content_type=XLSX,
        size_bytes=1024,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=summary,
    )


async def _guardar(db_session: AsyncSession, record: UploadedFile) -> UploadedFile:
    db_session.add(record)
    await db_session.commit()
    return record


def _summary_historico(**buckets: Any) -> dict[str, Any]:
    """Un summary como los que persistía el parser ANTES del mapeo universal.

    Lo que lo define es la ausencia de `mapping_contexts` con `inferred_type`
    mixto: esa combinación es la que despacha al camino multihoja y cae en su
    rama legacy.
    """
    return {
        "confidence": "HIGH",
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "row_count": sum(len(v) for v in buckets.values()),
        **buckets,
    }


async def _cuantos(db_session: AsyncSession, modelo: Any, tenant_id: Any) -> int:
    total = await db_session.execute(
        select(func.count()).select_from(modelo).where(modelo.tenant_id == tenant_id)
    )
    return int(total.scalar_one())


@pytest_asyncio.fixture
async def historico_con_ventas(
    db_session: AsyncSession, sample_tenant: Tenant
) -> UploadedFile:
    return await _guardar(
        db_session,
        _archivo(
            sample_tenant.tenant_id,
            _summary_historico(
                ventas_detectadas=[
                    {"Fecha": "01/03/2026", "Producto": "Coca 500", "Monto": "1500"},
                    {"Fecha": "02/03/2026", "Producto": "Agua", "Monto": "800"},
                ]
            ),
        ),
    )


@pytest_asyncio.fixture
async def historico_con_gastos(
    db_session: AsyncSession, sample_tenant: Tenant
) -> UploadedFile:
    return await _guardar(
        db_session,
        _archivo(
            sample_tenant.tenant_id,
            _summary_historico(
                gastos_detectados=[
                    {"Fecha": "01/03/2026", "Proveedor": "Distri SA", "Total": "9000"},
                ]
            ),
        ),
    )


class TestElHistoricoConOperacionesNoSeImporta:
    async def test_ventas_se_rechaza_y_no_persiste_nada(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        historico_con_ventas: UploadedFile,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{historico_con_ventas.id}/confirm",
            json={
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                    {"source_column": "Monto", "target_field": "amount"},
                ],
                "confirmed_fields": {"ventas": True},
            },
            headers=auth_headers,
        )

        assert response.status_code == 422, response.text
        detalle = response.json()["detail"]
        # La acción concreta: releer, NO volver a subir. El archivo está bien.
        assert "leer" in detalle.lower()
        assert "subir" not in detalle.lower()

        assert await _cuantos(db_session, SaleEntry, sample_tenant.tenant_id) == 0

    async def test_gastos_se_rechaza_y_no_persiste_nada(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        historico_con_gastos: UploadedFile,
    ) -> None:
        response = await client.post(
            f"/api/v1/ingestion/files/{historico_con_gastos.id}/confirm",
            json={
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "expense_date"},
                    {"source_column": "Total", "target_field": "amount"},
                ],
                "confirmed_fields": {"gastos": True},
            },
            headers=auth_headers,
        )

        assert response.status_code == 422, response.text
        assert await _cuantos(db_session, ExpenseEntry, sample_tenant.tenant_id) == 0

    async def test_el_rechazo_no_depende_de_que_el_usuario_pida_esas_filas(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        historico_con_ventas: UploadedFile,
    ) -> None:
        """Si NO confirmó ventas, no hay operación que persistir: no se rechaza.

        Lo que dispara el bloqueo es que el import vaya a escribir operaciones sin
        identidad, no que el summary sea viejo. Un archivo histórico del que sólo
        se importan productos no tiene por qué rebotar.
        """
        response = await client.post(
            f"/api/v1/ingestion/files/{historico_con_ventas.id}/confirm",
            json={
                "column_mappings": [
                    {"source_column": "Producto", "target_field": "name"},
                ],
                "confirmed_fields": {"productos": True},
            },
            headers=auth_headers,
        )

        # Puede rebotar por otra cosa (no hay productos en este archivo), pero
        # NO por el bloqueo de identidad: no hay operación que escribir.
        detalle = response.json().get("detail") if response.status_code == 422 else ""
        assert "volvé a leer el archivo" not in str(detalle).lower()
        assert await _cuantos(db_session, SaleEntry, sample_tenant.tenant_id) == 0


class TestLoQueNoSeToca:
    async def test_un_archivo_vacio_no_cae_en_este_rechazo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El parser de hoy sí produce summaries sin contextos: los de archivos
        vacíos. No tienen una sola fila, así que no pueden duplicar nada."""
        vacio = await _guardar(
            db_session,
            _archivo(
                sample_tenant.tenant_id,
                _summary_historico(ventas_detectadas=[], gastos_detectados=[]),
                nombre="vacio.xlsx",
            ),
        )

        response = await client.post(
            f"/api/v1/ingestion/files/{vacio.id}/confirm",
            json={
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                    {"source_column": "Monto", "target_field": "amount"},
                ],
                "confirmed_fields": {"ventas": True},
            },
            headers=auth_headers,
        )

        detalle = response.json().get("detail") if response.status_code == 422 else ""
        assert "leer el archivo" not in str(detalle), (
            "un archivo vacío no tiene operaciones que puedan duplicarse"
        )

    async def test_un_import_solo_de_productos_sigue_andando(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """Un producto no es una operación: no tiene identidad de comprobante que
        perder, y bloquearlo sería romper un import que hoy funciona."""
        catalogo = await _guardar(
            db_session,
            _archivo(
                sample_tenant.tenant_id,
                _summary_historico(
                    stock_detectado=[
                        {"Producto": "Coca 500", "Precio": "1500", "Cantidad": "10"},
                    ]
                ),
                nombre="catalogo.xlsx",
            ),
        )

        response = await client.post(
            f"/api/v1/ingestion/files/{catalogo.id}/confirm",
            json={
                "column_mappings": [
                    {"source_column": "Producto", "target_field": "name"},
                    {"source_column": "Precio", "target_field": "sale_price_ars"},
                    {"source_column": "Cantidad", "target_field": "stock_units"},
                ],
                "confirmed_fields": {"productos": True},
            },
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text
        assert await _cuantos(db_session, Product, sample_tenant.tenant_id) == 1

    async def test_una_tabla_suelta_sin_contextos_se_importa_igual(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """El camino plano NO es el camino bloqueado, y la diferencia es medible:
        llama a `planificar_identidades` con las columnas que mapeó el usuario.

        Sin esta prueba el bloqueo se podría ampliar de más y romper el caso
        histórico más común, que es justamente el que sí está protegido.
        """
        plana = await _guardar(
            db_session,
            _archivo(
                sample_tenant.tenant_id,
                {
                    "confidence": "HIGH",
                    "file_type": "spreadsheet",
                    "inferred_type": "ventas",
                    "row_count": 1,
                    "ventas_detectadas": [
                        {"Fecha": "01/03/2026", "Producto": "Coca", "Monto": "1500"},
                    ],
                },
                nombre="tabla.xlsx",
            ),
        )

        response = await client.post(
            f"/api/v1/ingestion/files/{plana.id}/confirm",
            json={
                "column_mappings": [
                    {"source_column": "Fecha", "target_field": "transaction_date"},
                    {"source_column": "Monto", "target_field": "amount"},
                ],
                "confirmed_fields": {"ventas": True},
            },
            headers=auth_headers,
        )

        assert response.status_code == 200, response.text
        assert await _cuantos(db_session, SaleEntry, sample_tenant.tenant_id) == 1

    async def test_un_documento_de_texto_sigue_yendo_a_otros(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        """La otra rama legacy —documentos— no persiste operaciones: rutea cada
        línea a «Otros» para revisión. No hay nada que bloquear ahí."""
        documento = await _guardar(
            db_session,
            _archivo(
                sample_tenant.tenant_id,
                {
                    "confidence": "MEDIUM",
                    "file_type": "text",
                    "row_count": 1,
                    "ventas_detectadas": [
                        {"linea": "Venta Coca $1500", "montos": ["$1500"]}
                    ],
                },
                nombre="remito.txt",
            ),
        )

        response = await client.post(
            f"/api/v1/ingestion/files/{documento.id}/confirm",
            json={"column_mappings": [], "confirmed_fields": {"ventas": True}},
            headers=auth_headers,
        )

        # Lo que este bloqueo tiene que dejar intacto: que un documento NO
        # persista operaciones. No las persiste — las rutea a «Otros», así que
        # no hay identidad que perder y el rechazo no aplica.
        detalle = response.json().get("detail") if response.status_code == 422 else ""
        assert "volvé a leer el archivo" not in str(detalle).lower()
        assert await _cuantos(db_session, SaleEntry, sample_tenant.tenant_id) == 0
        assert await _cuantos(db_session, ExpenseEntry, sample_tenant.tenant_id) == 0


class TestLaSalidaOfrecidaFunciona:
    async def test_la_relectura_desde_el_estado_historico_devuelve_contextos(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        sample_tenant: Tenant,
        historico_con_ventas: UploadedFile,
    ) -> None:
        """El rechazo manda a releer: hay que probar que releer DESDE ESE ESTADO
        devuelve contextos, no sólo que el parser nuevo los genera en abstracto.

        Es la diferencia entre "la salida existe" y "la salida existe para este
        archivo": el run de relectura arranca del registro guardado, con su
        summary viejo, su `ingestion_version` vieja y su hash.
        """
        import io

        from openpyxl import Workbook

        libro = Workbook()
        hoja = libro.active
        assert hoja is not None
        hoja.title = "Ventas"
        hoja.append(["Fecha", "Producto", "Monto"])
        hoja.append(["01/03/2026", "Coca 500", 1500])
        hoja.append(["02/03/2026", "Agua", 800])
        buffer = io.BytesIO()
        libro.save(buffer)
        contenido = buffer.getvalue()

        with (
            unittest.mock.patch(
                "app.integrations.s3.S3Client.download",
                new_callable=unittest.mock.AsyncMock,
                return_value=contenido,
            ),
            unittest.mock.patch(
                "app.integrations.s3.S3Client.head",
                new_callable=unittest.mock.AsyncMock,
                return_value={"etag": "abc", "size": len(contenido)},
            ),
        ):
            preview = await client.post(
                f"/api/v1/ingestion/files/{historico_con_ventas.id}/reread/preview",
                json={},
                headers=auth_headers,
            )

        assert preview.status_code == 200, preview.text

        await db_session.refresh(historico_con_ventas)
        cuerpo = preview.json()
        contextos = cuerpo.get("mapping_contexts")
        assert contextos, (
            "la relectura que ofrece el rechazo tiene que devolver contextos: "
            f"devolvió {contextos!r}"
        )
        # Y los devuelve por lectura real del archivo, no por el fallback que
        # inventa un contexto único cuando no puede interpretarlo.
        assert cuerpo.get("legacy_fallback") is False


class TestElPredicadoEspejaElDespacho:
    """El predicado tiene que decidir con las MISMAS condiciones que eligen el
    camino multihoja en `insert_confirmed_data`. Si divergiera, el bloqueo
    protegería un camino distinto del que persiste — parecería aplicado sin
    aplicar. Estos casos fijan ese espejo sobre la función pura, porque el
    recorrido HTTP no los alcanza: son formas de summary que el parser de hoy no
    emite, y de eso se trata — el guard no puede depender de que nunca aparezcan.
    """

    def test_un_documento_no_entra_aunque_venga_marcado_como_mixto(self) -> None:
        from app.domain.legacy_import_guard import importaria_operaciones_sin_identidad

        documento = {
            "file_type": "text",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "ventas_detectadas": [{"linea": "Venta", "montos": ["$1500"]}],
        }
        # El despacho manda los documentos a la rama de texto, que rutea a
        # «Otros» y no persiste operaciones. Bloquearlo sería rechazar un import
        # que no tiene identidad que perder.
        assert not importaria_operaciones_sin_identidad(documento, {"ventas": True})

    def test_una_planilla_en_las_mismas_condiciones_si_entra(self) -> None:
        from app.domain.legacy_import_guard import importaria_operaciones_sin_identidad

        planilla = {
            "file_type": "spreadsheet",
            "inferred_type": "mixed",
            "multi_sheet": True,
            "ventas_detectadas": [{"Fecha": "01/03/2026", "Monto": "1500"}],
        }
        assert importaria_operaciones_sin_identidad(planilla, {"ventas": True})
