"""El incidente de ASTERIA (2026-10-05), recorrido por el guardado REAL del worker.

``process_spreadsheet`` reventaba al guardar el resumen: la marca de fórmula sin
calcular llevaba ``\\x00`` y Postgres lo rechaza en JSONB
(``UntranslatableCharacterError``). En SQLite se guarda sin problema, así que la
suite nunca lo vio. Va contra Postgres real y llama a la task entera —claim,
parseo, gate, ``_save_result``— con el engine que la task se arma sola.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
from typing import Any

import pytest
from openpyxl import Workbook
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import NullPool

from app.domain.ingestion_limits import es_formula_sin_resultado
from app.jobs import ingestion_worker as worker
from app.persistence.db.engine_factory import crear_engine
from app.persistence.models.file import (
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_PENDING,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _libro_como_el_de_asteria() -> bytes:
    """Lo que tenía el archivo real, reducido: columnas calculadas que nunca se
    calcularon (openpyxl escribe la fórmula sin resultado, como cualquier
    script) y una hoja de resumen con flujo y saldo negativos."""
    wb = Workbook()
    precios = wb.active
    precios.title = "Precios y stock"
    precios.append(["Producto", "Stock", "Precio de compra", "Envio", "compra+envio"])
    for i in range(2, 8):
        # La última queda fuera del subconjunto que se calcula (Fase 3): su marca
        # es la que tiene que poder guardarse y leerse de vuelta.
        precios.append([f"Vaso {i}", i, 1000 * i, f"=C{i}*0.1", f"=VLOOKUP(C{i},A1:B5,2)"])
    resumen = wb.create_sheet("Resumen mensual")
    resumen.append(["Fecha", "Ventas $", "Flujo neto de caja $", "Saldo de caja acumulado $"])
    resumen.append(["2026-06-30", 755300, -119969.18, 12000])
    resumen.append(["2026-07-31", 1402758.64, -246587.14, -3363.49])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _correr(corutina: Any) -> Any:
    return asyncio.run(corutina)


def _sm() -> Any:
    assert TEST_PG_DSN is not None
    engine = crear_engine(TEST_PG_DSN, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _sembrar(tenant_id: uuid.UUID) -> uuid.UUID:
    engine, sm = _sm()
    file_id = uuid.uuid4()
    try:
        async with sm() as s:
            s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
            await s.flush()
            s.add(
                UploadedFile(
                    id=file_id,
                    tenant_id=tenant_id,
                    original_filename="ASTERIA_home_deco_ordenado.xlsx",
                    s3_key="k",
                    content_type=_XLSX,
                    size_bytes=1,
                    purpose="ventas",
                    processing_status=PROCESSING_STATUS_PENDING,
                )
            )
            await s.commit()
    finally:
        await engine.dispose()
    return file_id


async def _leer(file_id: uuid.UUID) -> tuple[str, dict[str, Any] | None]:
    engine, sm = _sm()
    try:
        async with sm() as s:
            fila = (
                await s.execute(
                    select(
                        UploadedFile.processing_status, UploadedFile.parsed_summary_json
                    ).where(UploadedFile.id == file_id)
                )
            ).one()
    finally:
        await engine.dispose()
    return fila.processing_status, fila.parsed_summary_json


async def _limpiar(tenant_id: uuid.UUID) -> None:
    engine, sm = _sm()
    try:
        async with sm() as s:
            await s.execute(delete(UploadedFile).where(UploadedFile.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()
    finally:
        await engine.dispose()


@pytest.fixture
def _worker_contra_pg(monkeypatch: pytest.MonkeyPatch) -> None:
    """La task se arma su engine con la función REAL (y por lo tanto con el
    serializador de la fábrica), apuntada al Postgres de prueba."""
    assert TEST_PG_DSN is not None
    dsn = TEST_PG_DSN
    original = worker._build_async_session
    monkeypatch.setattr(worker, "_build_async_session", lambda _url: original(dsn))


def _s3_que_devuelve(contenido: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.integrations.s3 as s3_mod

    class _S3:
        async def download(self, _key: str) -> bytes:
            return contenido

    monkeypatch.setattr(s3_mod, "S3Client", _S3)


def test_un_libro_con_formulas_sin_calcular_se_guarda(
    monkeypatch: pytest.MonkeyPatch, _worker_contra_pg: None
) -> None:
    """El archivo llega a un estado de revisión, con la marca guardada y
    reconocible al leerla de vuelta — que es lo que hace el importador."""
    tenant_id = uuid.uuid4()
    file_id = _correr(_sembrar(tenant_id))
    _s3_que_devuelve(_libro_como_el_de_asteria(), monkeypatch)
    try:
        worker.process_spreadsheet(str(file_id), str(tenant_id))

        estado, summary = _correr(_leer(file_id))
        assert estado != PROCESSING_STATUS_FAILED, summary
        assert summary is not None
        assert summary["formulas_sin_resultado"]
        valores = [
            v
            for clave in ("preview_rows", "stock_detectado", "ventas_detectadas")
            for fila in summary.get(clave, [])
            if isinstance(fila, dict)
            for v in fila.values()
        ]
        assert any(es_formula_sin_resultado(v) for v in valores)
    finally:
        _correr(_limpiar(tenant_id))


def test_un_saldo_negativo_no_rechaza_el_archivo(
    monkeypatch: pytest.MonkeyPatch, _worker_contra_pg: None
) -> None:
    """Antes el gate rechazaba el archivo entero (`negative_amount`) por el flujo
    y el saldo del resumen, que son datos y no montos de venta."""
    tenant_id = uuid.uuid4()
    file_id = _correr(_sembrar(tenant_id))
    _s3_que_devuelve(_libro_como_el_de_asteria(), monkeypatch)
    try:
        worker.process_spreadsheet(str(file_id), str(tenant_id))
        estado, summary = _correr(_leer(file_id))
        assert estado != "REJECTED", summary
    finally:
        _correr(_limpiar(tenant_id))


def test_un_nulo_que_llega_al_guardado_cierra_en_failed_con_motivo(
    monkeypatch: pytest.MonkeyPatch, _worker_contra_pg: None
) -> None:
    """La red de salida: si un camino que no normaliza deja pasar un nulo, el
    archivo termina en FAILED con un motivo en castellano y un código — nunca con
    el texto de asyncpg, y nunca trabado en PROCESSING."""
    tenant_id = uuid.uuid4()
    file_id = _correr(_sembrar(tenant_id))
    _s3_que_devuelve(b"irrelevante", monkeypatch)

    def _parseo_con_nulo(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "file_type": "spreadsheet",
            "confidence": "HIGH",
            "warnings": [],
            "inferred_type": "stock",
            "has_producto": True,
            "stock_detectado": [{"producto": "Va\x00so", "stock": "3"}],
            "preview_rows": [{"producto": "Va\x00so", "stock": "3"}],
            "mapping_contexts": [],
        }

    monkeypatch.setattr(worker, "parse_uploaded_content", _parseo_con_nulo)
    try:
        with pytest.raises(Exception, match="nulo"):
            worker.process_spreadsheet(str(file_id), str(tenant_id))
        estado, summary = _correr(_leer(file_id))
        assert estado == PROCESSING_STATUS_FAILED
        assert summary is not None
        assert summary["error_code"] == worker.ERROR_RESULTADO_NO_PERSISTIBLE
        assert "asyncpg" not in summary["error"]
        assert "Untranslatable" not in summary["error"]
    finally:
        _correr(_limpiar(tenant_id))
