"""F-R — la relectura tiene que probar su correspondencia antes de anular nada.

**Lo que se midió antes de escribir la fase** (`reread_service.py`, preview):
`to_void = len(recon.non_edited)` y `to_update` cuenta las filas frescas cuya
huella está entre las que se van a anular. O sea que los dos números cuentan **las
mismas filas**: anular y reimportar corregido *es* el mecanismo de actualizar. En
la corrida real de ASTERIA daban `2563 / 2563`, y no había pérdida.

El defecto no es el número: es que **nada lo garantizaba**. `to_update` sale del
parse NUEVO, así que si el archivo cambia —una hoja que pasa a clasificarse como
catálogo, un mapeo distinto, filas que desaparecen— `to_void` se queda igual y
`to_update` cae, y hasta acá ese apply se ejecutaba sin que nadie preguntara.

Estos tests cubren los tres casos que distinguen una actualización de una pérdida:

| escenario                          | reemplazado | sin reemplazo | ¿bloquea? |
|------------------------------------|-------------|---------------|-----------|
| el mismo archivo, sin cambios      | 3           | 0             | no        |
| al archivo le falta una fila       | 2           | 1             | **sí**    |
| la hoja deja de leerse como ventas | 0           | 3             | **sí**    |
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.integrations.s3 import S3Client
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests.conftest import add_business_profile

_PRODUCTO = "Vela aromatica"

#: Tres ventas legibles. Las tres entran, las tres tienen `source_row_ref`.
_CSV_ORIGINAL = (
    b"fecha,producto,cantidad,monto,cliente\n"
    b"2026-01-05,Vela aromatica,3,1500,Ana\n"
    b"2026-01-06,Vela aromatica,2,900,Luis\n"
    b"2026-01-07,Vela aromatica,1,450,Sol\n"
)

#: El mismo archivo con la ÚLTIMA fila borrada. Se saca la última a propósito: el
#: ancla es (hoja, índice de fila), así que borrar la primera correría los índices
#: y las tres se verían distintas — el test estaría midiendo el corrimiento, no la
#: desaparición.
_CSV_FALTA_LA_ULTIMA = (
    b"fecha,producto,cantidad,monto,cliente\n"
    b"2026-01-05,Vela aromatica,3,1500,Ana\n"
    b"2026-01-06,Vela aromatica,2,900,Luis\n"
)

#: El archivo pasa a ser un catálogo: ni fecha ni monto. Es el escenario del bug
#: vivo de `has_catalogo_fuerte` —una hoja de ventas que se reclasifica como
#: catálogo por nombrar su columna «Artículo»— reducido a su consecuencia: el
#: parse nuevo no produce ninguna venta y las tres quedan sin reemplazo.
_CSV_YA_NO_ES_VENTAS = (
    b"producto,stock,precio\n"
    b"Vela aromatica,10,500\n"
    b"Difusor,4,900\n"
)


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada apply paga los reintentos de kombu."""


@pytest_asyncio.fixture
async def tenant(db_session: AsyncSession) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Deco Test",
        display_name="Deco Test",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(t)
    await db_session.flush()
    await add_business_profile(db_session, t.tenant_id)
    await db_session.commit()
    return t


@pytest_asyncio.fixture
async def producto(db_session: AsyncSession, tenant: Tenant) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name=_PRODUCTO,
        sale_price_ars=Decimal("500"),
        unit_cost_ars=Decimal("300"),
        stock_units=50,
    )
    db_session.add(p)
    await db_session.commit()
    return p


def _servir(monkeypatch: pytest.MonkeyPatch, contenido: bytes) -> None:
    """Qué bytes devuelve S3 cuando la relectura re-descarga el archivo.

    Es la palanca del test: cambiar esto es exactamente "el usuario subió otra
    versión del archivo" desde el punto de vista de la relectura.
    """

    async def _fake_download(self: S3Client, key: str) -> bytes:  # noqa: ARG001
        return contenido

    monkeypatch.setattr(S3Client, "download", _fake_download)


@pytest_asyncio.fixture
async def archivo(
    db_session: AsyncSession, tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> UploadedFile:
    _servir(monkeypatch, _CSV_ORIGINAL)
    summary = parse_uploaded_content(_CSV_ORIGINAL, "text/csv", "ventas.csv")
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.csv",
        s3_key=f"tenants/{tenant.tenant_id}/ventas.csv",
        content_type="text/csv",
        size_bytes=len(_CSV_ORIGINAL),
        purpose="ventas",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": summary.get("inferred_type"),
            "confirmed_fields": {"ventas": True},
        },
    )
    db_session.add(f)
    await db_session.commit()
    return f


@pytest_asyncio.fixture
async def importado(
    db_session: AsyncSession, tenant: Tenant, archivo: UploadedFile, producto: Product
) -> UploadedFile:
    summary = parse_uploaded_content(_CSV_ORIGINAL, "text/csv", "ventas.csv")
    await insert_confirmed_data(
        db_session,
        tenant.tenant_id,
        summary,
        {"ventas": True},
        source="ingestion",
        uploaded_file_id=archivo.id,
    )
    await db_session.commit()
    vivas = (
        (
            await db_session.execute(
                select(SaleEntry).where(SaleEntry.tenant_id == tenant.tenant_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(vivas) == 3, "el import de control tiene que dejar las 3 ventas"
    return archivo


class TestCorrespondencia:
    async def test_el_mismo_archivo_repone_todo_lo_que_anula(
        self,
        db_session: AsyncSession,
        tenant: Tenant,
        importado: UploadedFile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """El caso de ASTERIA: to_void y to_update son las mismas filas.

        Sin la correspondencia, ese `3 / 3` es indistinguible de "voy a borrar 3".
        """
        _servir(monkeypatch, _CSV_ORIGINAL)
        preview = await reread_service.preview_reread(
            db_session, importado.id, tenant.tenant_id
        )

        assert preview.to_void == 3
        assert preview.to_update == 3
        corr = preview.correspondence
        assert corr.reemplazado == 3
        assert corr.sin_reemplazo == 0
        assert corr.bloquea is False
        assert corr.por_entidad["ventas"]["antes"] == 3
        assert corr.por_entidad["ventas"]["reemplazado"] == 3
        assert corr.por_entidad["ventas"]["sin_reemplazo"] == 0

    async def test_sin_perdida_no_hay_tarjetas_de_anulado(
        self,
        db_session: AsyncSession,
        tenant: Tenant,
        importado: UploadedFile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lo primero que veía el usuario eran 3 tarjetas «Anulado → —».

        Eran los REEMPLAZADOS: su archivo entero presentado como una destrucción.
        La tarjeta sin contraparte queda sólo para lo que de verdad no vuelve.
        """
        _servir(monkeypatch, _CSV_ORIGINAL)
        preview = await reread_service.preview_reread(
            db_session, importado.id, tenant.tenant_id
        )

        acciones = [s["action"] for s in preview.sample_changes]
        assert "void" not in acciones, "nada se pierde: ninguna tarjeta de anulado"
        pares = [s for s in preview.sample_changes if s["action"] == "update"]
        assert pares, "tiene que mostrar el par antes/después de la misma fila"
        assert all(p["before"] and p["after"] for p in pares)

    async def test_una_fila_que_desaparece_es_una_perdida(
        self,
        db_session: AsyncSession,
        tenant: Tenant,
        importado: UploadedFile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _servir(monkeypatch, _CSV_FALTA_LA_ULTIMA)
        preview = await reread_service.preview_reread(
            db_session, importado.id, tenant.tenant_id
        )

        corr = preview.correspondence
        assert corr.reemplazado == 2
        assert corr.sin_reemplazo == 1
        assert corr.bloquea is True
        assert corr.por_entidad["ventas"]["sin_reemplazo"] == 1
        # Y la tarjeta de anulado aparece: es la única fila que no vuelve.
        voids = [s for s in preview.sample_changes if s["action"] == "void"]
        assert len(voids) == 1
        assert voids[0]["after"] is None
        assert str(voids[0]["before"]["amount"]) == "450.00"

    async def test_la_hoja_que_deja_de_ser_ventas_pierde_todo(
        self,
        db_session: AsyncSession,
        tenant: Tenant,
        importado: UploadedFile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """El escenario del bug vivo de `has_catalogo_fuerte`, en su consecuencia.

        El parse nuevo no produce ninguna venta: las 3 se anulan y ninguna vuelve.
        Antes de F-R esto se aplicaba sin preguntar.
        """
        _servir(monkeypatch, _CSV_YA_NO_ES_VENTAS)
        preview = await reread_service.preview_reread(
            db_session, importado.id, tenant.tenant_id
        )

        corr = preview.correspondence
        assert corr.sin_reemplazo == 3
        assert corr.reemplazado == 0
        assert corr.bloquea is True


class TestCompuertaDelApply:
    async def test_bloquea_el_apply_que_perderia_datos(
        self,
        db_session: AsyncSession,
        tenant: Tenant,
        importado: UploadedFile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _servir(monkeypatch, _CSV_FALTA_LA_ULTIMA)

        with pytest.raises(reread_service.RereadWouldLoseDataError) as exc:
            await reread_service.start_background_apply(
                db_session, importado.id, tenant.tenant_id
            )

        assert exc.value.correspondence.sin_reemplazo == 1
        assert "1 registro(s)" in str(exc.value)
        assert "ventas" in str(exc.value)

    async def test_el_bloqueo_no_deja_un_run_huerfano(
        self,
        db_session: AsyncSession,
        tenant: Tenant,
        importado: UploadedFile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Un RUNNING creado antes de la compuerta bloquearía el siguiente intento
        con «ya hay una relectura en curso» — el usuario quedaría trabado sin
        haber aplicado nada."""
        from app.persistence.models.repair import DataRepairRun

        _servir(monkeypatch, _CSV_FALTA_LA_ULTIMA)
        with pytest.raises(reread_service.RereadWouldLoseDataError):
            await reread_service.start_background_apply(
                db_session, importado.id, tenant.tenant_id
            )

        runs = (
            (
                await db_session.execute(
                    select(DataRepairRun).where(DataRepairRun.tenant_id == tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert runs == []

    async def test_con_aceptacion_explicita_sigue_adelante(
        self,
        db_session: AsyncSession,
        tenant: Tenant,
        importado: UploadedFile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Anular sin reponer es legítimo: el archivo pudo haber cambiado de
        verdad. Lo que no puede es pasar en silencio."""
        _servir(monkeypatch, _CSV_FALTA_LA_ULTIMA)

        run = await reread_service.start_background_apply(
            db_session, importado.id, tenant.tenant_id, accept_data_loss=True
        )

        assert run.status == "RUNNING"

    async def test_sin_perdida_no_hace_falta_aceptar_nada(
        self,
        db_session: AsyncSession,
        tenant: Tenant,
        importado: UploadedFile,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _servir(monkeypatch, _CSV_ORIGINAL)

        run = await reread_service.start_background_apply(
            db_session, importado.id, tenant.tenant_id
        )

        assert run.status == "RUNNING"
