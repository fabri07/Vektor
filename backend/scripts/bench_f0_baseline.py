"""F0 — línea de base del importador con historial del tenant CRECIENTE.

Por qué esta medición y no otra
-------------------------------
Los benchmarks que ya existen (``bench_confirm_import``, ``bench_reread_apply``,
la compuerta de ``test_ingestion_statement_budget_pg``) miden un import sobre un
tenant **vacío**. Eso responde "¿hay un N+1 por fila?" y no responde la pregunta
que F0 dejó abierta y que E6c tiene que cerrar: **¿qué pasa cuando el tenant ya
tiene historia?**

La hipótesis H08 es concreta y está en el código: ``_load_import_fingerprints``
hace un ``SELECT`` sin filtro por archivo y materializa en un ``set`` de Python
**todas las huellas de import del tenant**, para todos sus archivos de todos los
tiempos. El costo de importar 100 filas no depende de las 100 filas: depende de
cuánto importó el negocio antes. Un tenant en su primer mes y el mismo tenant dos
años después pagan cosas distintas por el mismo archivo.

Esta es la referencia contra la que se va a evaluar el acotamiento de la
deduplicación de E6c ("el consumo no debe crecer con todas las huellas históricas
del tenant"). Sin el número previo, "mejoró" no se puede afirmar.

Qué mide
--------
Matriz de *filas del archivo* × *huellas históricas del tenant*, y por celda:

* **statements totales y por forma de SQL** — el conteo es lo que importa contra
  Neon, donde cada uno cuesta 30-50 ms de latencia (local son ~0,1 ms: los
  segundos de acá son un piso, no una estimación).
* **memoria pico** del proceso durante el import (``tracemalloc``), que es la
  dimensión donde H08 duele y que ningún bench del repo estaba mirando.
* **tiempo de pared**, con la advertencia de arriba.
* **filas efectivamente importadas**, para que una celda "rápida" que no importó
  nada no se lea como una mejora.

Qué NO mide, dicho para que no se lea como completo
---------------------------------------------------
El desglose por ETAPA del endpoint (espera de cola, parsing, preview,
validaciones, publicación) lo produce ``StageTimings`` dentro de ``confirm_file``
y ya tiene su herramienta: ``bench_confirm_import.py``. Necesita el archivo real
y credenciales de R2, así que no corre acá. Este script mide el importador —la
etapa dominante—, no el endpoint entero.

Uso
---
    export TEST_PG_DSN=postgresql+asyncpg://...   # Postgres LOCAL/descartable
    .venv/bin/python scripts/bench_f0_baseline.py
    .venv/bin/python scripts/bench_f0_baseline.py --rapido   # matriz chica
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import tracemalloc
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from app.application.services.ingestion_import_service import (  # noqa: E402
    insert_confirmed_data,
)
from app.persistence.models.business import BusinessProfile  # noqa: E402
from app.persistence.models.customer import Customer  # noqa: E402
from app.persistence.models.file import UploadedFile  # noqa: E402
from app.persistence.models.inventory import (  # noqa: E402
    InventoryBalance,
    InventoryMovement,
)
from app.persistence.models.memory import OperationFingerprint  # noqa: E402
from app.persistence.models.operation_identity import (  # noqa: E402
    OperationIdentity,
    OperationIdentityLink,
)
from app.persistence.models.product import Product  # noqa: E402
from app.persistence.models.supplier import Supplier  # noqa: E402
from app.persistence.models.tenant import Tenant  # noqa: E402
from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: E402
from app.persistence.models.unclassified_record import UnclassifiedRecord  # noqa: E402
from scripts._bench_sql import SqlProfile, attach, p  # noqa: E402

_CTX = "sheet:Compras"
_COLS = {
    "fecha": "expense_date",
    "nro": "invoice_number",
    "proveedor": "supplier_name",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
}

#: El ``action_type`` con el que el importador registra y precarga sus huellas de
#: fila. Se importa del servicio para que sembrar historia falsa no pueda
#: divergir de lo que el import realmente lee.
from app.application.services.ingestion_import_service import (  # noqa: E402
    _IMPORT_ROW_ACTION,
)


def _summary(n_filas: int) -> dict[str, Any]:
    """Un libro de compras: 10 líneas por comprobante, como una planilla real."""
    filas = [
        {
            "fecha": "2024-03-05",
            "nro": f"{i // 10:08d}",
            "proveedor": f"Prov {i // 10 % 20}",
            "articulo": f"Art {i}",
            "cantidad": "2",
            "total": "6000",
            "__context__": _CTX,
        }
        for i in range(n_filas)
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "multi_sheet": True,
        "row_count": n_filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": list(_COLS),
                "row_count": n_filas,
            }
        ],
        "gastos_detectados": filas,
    }


async def _sembrar_historial(
    session: AsyncSession, tenant_id: uuid.UUID, cuantas: int
) -> None:
    """Huellas de import de archivos VIEJOS del tenant.

    Son huellas que este import no va a usar nunca —vienen de otros archivos—,
    que es exactamente el punto: ``_load_import_fingerprints`` las trae igual.
    Se insertan en lotes para no volver la siembra el cuello de botella del
    propio benchmark.
    """
    if not cuantas:
        return
    lote = 5_000
    for inicio in range(0, cuantas, lote):
        session.add_all(
            [
                OperationFingerprint(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    fingerprint=f"historico-{inicio + j:012d}-{uuid.uuid4().hex}",
                    action_type=_IMPORT_ROW_ACTION,
                )
                for j in range(min(lote, cuantas - inicio))
            ]
        )
        await session.flush()
    await session.commit()


async def _preparar(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    session.add(Tenant(tenant_id=tenant_id, legal_name="Bench", display_name="Bench"))
    await session.flush()
    session.add(
        BusinessProfile(
            profile_id=uuid.uuid4(),
            tenant_id=tenant_id,
            vertical_code="kiosco_almacen",
            data_mode="M0",
            data_confidence="LOW",
            onboarding_completed=True,
        )
    )
    upload_id = uuid.uuid4()
    session.add(
        UploadedFile(
            id=upload_id,
            tenant_id=tenant_id,
            original_filename="compras.xlsx",
            s3_key=f"bench/{upload_id}.xlsx",
            content_type="text/csv",
            size_bytes=1,
            purpose="ingestion",
            status="uploaded",
            processing_status="DONE",
        )
    )
    await session.commit()
    return upload_id


async def _limpiar(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    async with factory() as s:
        for modelo in (
            InventoryMovement,
            InventoryBalance,
            UnclassifiedRecord,
            OperationIdentityLink,
            SaleEntry,
            ExpenseEntry,
            Product,
            Supplier,
            Customer,
            OperationFingerprint,
            OperationIdentity,
            BusinessProfile,
            UploadedFile,
        ):
            await s.execute(delete(modelo).where(modelo.tenant_id == tenant_id))
        await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
        await s.commit()


async def _celda(
    engine: AsyncEngine,
    factory: async_sessionmaker[AsyncSession],
    *,
    n_filas: int,
    historial: int,
) -> dict[str, Any]:
    tenant_id = uuid.uuid4()
    perfil = SqlProfile()
    try:
        async with factory() as session:
            upload_id = await _preparar(session, tenant_id)
            await _sembrar_historial(session, tenant_id, historial)

            attach(engine, perfil)
            perfil.enabled = True
            tracemalloc.start()
            t0 = time.perf_counter()
            counts = await insert_confirmed_data(
                session,
                tenant_id,
                _summary(n_filas),
                {},
                context_mappings={_CTX: dict(_COLS)},
                context_entity={_CTX: "expense"},
                context_confirmed={_CTX: True},
                source="ingestion",
                uploaded_file_id=upload_id,
            )
            await session.commit()
            segundos = time.perf_counter() - t0
            _, pico = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            perfil.enabled = False
        return {
            "filas": n_filas,
            "historial": historial,
            "statements": perfil.total,
            "segundos": segundos,
            "pico_mb": pico / 1024 / 1024,
            "gastos": counts.get("gastos", 0),
            "formas": dict(perfil.counts),
        }
    finally:
        await _limpiar(factory, tenant_id)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rapido", action="store_true", help="matriz chica (para iterar el script)"
    )
    args = parser.parse_args()

    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        raise SystemExit("Falta TEST_PG_DSN (Postgres LOCAL/descartable).")

    filas = [100, 1_000] if args.rapido else [100, 1_000, 5_000]
    historiales = [0, 20_000] if args.rapido else [0, 20_000, 100_000]

    engine = create_async_engine(dsn, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    resultados: list[dict[str, Any]] = []
    try:
        for n in filas:
            for h in historiales:
                r = await _celda(engine, factory, n_filas=n, historial=h)
                resultados.append(r)
                print(
                    f"  {r['filas']:>6} filas  historial {r['historial']:>7}  →  "
                    f"{r['statements']:>5} stmts  {r['segundos']:>6.2f}s  "
                    f"pico {r['pico_mb']:>6.1f} MB  ({r['gastos']} gastos)"
                )
    finally:
        await engine.dispose()

    p("F0 — cómo crece el costo con el HISTORIAL del tenant")
    print("  (mismo archivo, distinta historia previa)")
    for n in filas:
        base = next(r for r in resultados if r["filas"] == n and r["historial"] == 0)
        for h in historiales:
            r = next(x for x in resultados if x["filas"] == n and x["historial"] == h)
            x_stmt = r["statements"] / max(base["statements"], 1)
            x_mem = r["pico_mb"] / max(base["pico_mb"], 0.01)
            print(
                f"  {n:>6} filas | historial {h:>7} | "
                f"stmts {r['statements']:>5} (x{x_stmt:.2f}) | "
                f"pico {r['pico_mb']:>6.1f} MB (x{x_mem:.2f}) | "
                f"{r['segundos']:>6.2f}s"
            )

    p("Lectura")
    print(
        "  Si los statements NO crecen con el historial pero la MEMORIA sí, el\n"
        "  cuello es la precarga de huellas (`_load_import_fingerprints`), que es\n"
        "  una query pero materializa todo el historial del tenant en un set de\n"
        "  Python. Es la hipótesis H08 y lo que E6c tiene que acotar."
    )


if __name__ == "__main__":
    asyncio.run(main())
