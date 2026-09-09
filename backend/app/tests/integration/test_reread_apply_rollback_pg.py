"""E6b — qué queda guardado cuando ``apply_reread`` falla a mitad.

Lo que este módulo afirma NO es que hoy exista corrupción. El plan pedía "la
prueba dedicada de rollback de `apply_reread`, con fallas inyectadas", y lo que
faltaba era la MEDICIÓN: sin ella, ni "queda medio estado" ni "revierte todo" son
afirmaciones sostenidas. Estos tests inyectan el fallo en tres puntos donde ya se
escribió algo, dejan que la transacción se deshaga como en producción, y
**consultan desde una sesión nueva** qué quedó.

Por qué desde otra sesión, y por qué PostgreSQL
-----------------------------------------------
Preguntarle a la misma sesión que acaba de hacer rollback no prueba nada: la
identity map ya no tiene los objetos y la respuesta saldría de la nada. Lo que
importa es lo que ve una conexión distinta después, que es lo que va a ver el
próximo request. Y va contra Postgres real porque una garantía de atomicidad
sobre SQLite mide la emulación de transacciones de pysqlite, no la del motor de
producción (ver ``[[feedback_sqlite_masks_postgres]]``).

Los tres puntos de inyección están elegidos por lo que ya escribieron cuando
fallan, no por dónde es cómodo pinchar:

* **antes de la reconciliación** — el ``DataRepairRun`` del apply ya existe;
* **después de los voids** — las filas viejas ya se anularon y el reimport no
  llegó a reponerlas: es el estado más peligroso posible, el que dejaría al
  tenant sin sus gastos;
* **después de reconciliar todo** — gastos, productos, stock, movimientos y
  proveedor ya reescritos, y falla el sellado final del archivo.

Se compara un snapshot COMPLETO —gastos con su ``voided_at``, productos con
stock y costo, proveedores, movimientos de inventario, el estado de relectura del
archivo y los runs— antes y después. Un rollback que revierte los gastos pero
deja el stock movido no lo detectaría una aserción sobre una sola tabla.

Gating: se skippea limpio sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_reread_apply_rollback_pg.py -v --no-cov -n 0
"""

from __future__ import annotations

import inspect
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services import reread_service
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.verticals import Vertical
from app.persistence.db.base import Base
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

#: Namespace propio para serializar el DDL entre workers de xdist.
_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F6B  # "VEKTOR" + E6b

_PRODUCTO = "Vela aromatica 200g"
_PROVEEDOR = "Distribuidora Sur"
_HEADERS = ["fecha", "articulo", "cantidad", "total", "proveedor"]


def _summary(total: str) -> dict[str, Any]:
    """Una compra de mercadería: toca las cinco cosas que hay que comparar.

    Una hoja de gastos con producto, cantidad y proveedor crea —en un solo
    import— el gasto, el producto, su costo, el movimiento de inventario y el
    maestro del proveedor. Es el archivo más chico que ejerce todo lo que el
    rollback tiene que revertir.
    """
    filas = [
        {
            "fecha": "2024-03-05",
            "articulo": _PRODUCTO,
            "cantidad": "5",
            "total": total,
            "proveedor": _PROVEEDOR,
        }
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "multi_sheet": False,
        "has_gasto": True,
        "row_count": 1,
        "headers": _HEADERS,
        "gastos_detectados": filas,
        "preview_rows": filas,
        "confirmed_fields": {"gastos": True},
    }


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine asyncpg propio (NullPool → cada sesión, su conexión física).

    Sólo las tablas que este módulo consulta, igual criterio que el resto de los
    ``*_pg``: contra el schema ya migrado el ``checkfirst`` queda no-op, y pedir
    ``create_all`` de TODO el metadata falla —lo hace— porque hay modelos cuyo
    DDL del ORM no coincide con lo que construyó la cadena de migraciones.
    El import real escribe además en tablas que no se listan acá (balances,
    huellas, auditoría); todas existen en una base migrada, que es el requisito
    declarado de estos tests.
    """
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    tablas: list[Table] = [
        cast("Table", modelo.__table__)
        for modelo in (
            Tenant,
            BusinessProfile,
            UploadedFile,
            DataRepairRun,
            Supplier,
            Product,
            ExpenseEntry,
            InventoryMovement,
        )
    ]
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        await conn.run_sync(Base.metadata.create_all, tables=tablas, checkfirst=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def sm(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        async with factory() as s:
            for modelo in (
                InventoryMovement,
                ExpenseEntry,
                Product,
                Supplier,
                DataRepairRun,
                UploadedFile,
                BusinessProfile,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _estado(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> dict[str, Any]:
    """Todo lo que la relectura puede tocar, leído en una sesión NUEVA.

    Se listan ordenados y con los campos que un cambio silencioso movería —el
    ``voided_at`` de cada gasto, el stock y el costo de cada producto, el signo y
    la cantidad de cada movimiento—. Contar filas no alcanza: una relectura que
    anula un gasto y crea otro deja el mismo total.
    """
    async with factory() as s:
        gastos = (
            (
                await s.execute(
                    select(ExpenseEntry)
                    .where(ExpenseEntry.tenant_id == tenant_id)
                    .order_by(ExpenseEntry.id)
                )
            )
            .scalars()
            .all()
        )
        productos = (
            (
                await s.execute(
                    select(Product).where(Product.tenant_id == tenant_id).order_by(Product.name)
                )
            )
            .scalars()
            .all()
        )
        proveedores = (
            (
                await s.execute(
                    select(Supplier)
                    .where(Supplier.tenant_id == tenant_id)
                    .order_by(Supplier.name)
                )
            )
            .scalars()
            .all()
        )
        movimientos = (
            (
                await s.execute(
                    select(InventoryMovement)
                    .where(InventoryMovement.tenant_id == tenant_id)
                    .order_by(InventoryMovement.id)
                )
            )
            .scalars()
            .all()
        )
        archivo = (
            await s.execute(select(UploadedFile).where(UploadedFile.tenant_id == tenant_id))
        ).scalar_one()
        runs = (
            (
                await s.execute(
                    select(DataRepairRun)
                    .where(DataRepairRun.tenant_id == tenant_id)
                    .order_by(DataRepairRun.created_at)
                )
            )
            .scalars()
            .all()
        )
        return {
            "gastos": [
                (str(g.id), str(g.amount), g.voided_at is not None, str(g.product_id))
                for g in gastos
            ],
            "productos": [
                (p.name, int(p.stock_units or 0), str(p.unit_cost_ars), p.requires_completion)
                for p in productos
            ],
            "proveedores": [(p.name, p.deactivated_at is None) for p in proveedores],
            "movimientos": [
                (str(m.movement_type), int(m.qty), str(m.unit_cost)) for m in movimientos
            ],
            "archivo": (
                archivo.reread_status,
                archivo.ingestion_version,
                archivo.reread_at is not None,
                archivo.reread_summary,
            ),
            "runs": [(r.status, r.repair_type) for r in runs],
        }


async def _importar_la_primera_vez(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> uuid.UUID:
    """El estado de partida: un import normal, COMMITEADO.

    Tiene que estar commiteado de verdad — si el baseline viviera en la misma
    transacción que el apply, el rollback lo borraría también y el test estaría
    comparando dos vacíos.
    """
    file_id = uuid.uuid4()
    async with factory() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        # Sin perfil el import levanta `UnknownVerticalError`: desde que se
        # eliminaron los fallbacks de rubro, un tenant sin perfil es un estado
        # roto y los servicios prefieren fallar antes que asumir kiosco.
        s.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code=Vertical.KIOSCO_ALMACEN.value,
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=False,
            )
        )
        await s.flush()
        s.add(
            UploadedFile(
                id=file_id,
                tenant_id=tenant_id,
                original_filename="compras.xlsx",
                s3_key="k",
                content_type="application/vnd.ms-excel",
                size_bytes=1,
                purpose="gastos",
                processing_status=PROCESSING_STATUS_DONE,
                parsed_summary_json=_summary("6000"),
            )
        )
        await s.flush()
        await insert_confirmed_data(
            s,
            tenant_id,
            _summary("6000"),
            {"gastos": True},
            uploaded_file_id=file_id,
        )
        await s.commit()
    return file_id


async def _apply_con_fallo(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    *,
    romper: str,
) -> None:
    """Corre el apply con un fallo inyectado y deja que la transacción se deshaga.

    El ``rollback`` explícito es lo que hace el caller real: ``get_db_session``
    ante una excepción, y el worker de relectura en su ``except``. Sin él, el
    cierre del ``async with`` también revertiría — pero entonces el test estaría
    probando el contextmanager de SQLAlchemy y no el camino de producción.

    El sustituto respeta si el original era corrutina o no: ``build_reread_summary``
    es sync y devolver una corrutina desde ahí no levantaría nada, dejaría un
    objeto raro asignado a ``file.reread_summary`` y el test pasaría por el motivo
    equivocado.
    """
    original = getattr(reread_service, romper)
    es_async = inspect.iscoroutinefunction(original)

    def _explota_sync(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"fallo inyectado en {romper}")

    async def _explota_async(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"fallo inyectado en {romper}")

    monkeypatch.setattr(reread_service, romper, _explota_async if es_async else _explota_sync)

    async with factory() as s:
        with pytest.raises(RuntimeError, match="fallo inyectado"):
            await reread_service.apply_reread(
                s, file_id, tenant_id, fresh_override=_summary("9000")
            )
        await s.rollback()


async def test_el_apply_completo_si_cambia_el_estado(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Control del experimento, y el test que hace válidos a los otros.

    Si un apply exitoso no cambiara nada, "después del fallo quedó igual que
    antes" se cumpliría solo y los tests de abajo pasarían incluso con la
    transacción rota. Acá se comprueba que este escenario SÍ produce cambios
    visibles cuando termina bien.
    """
    file_id = await _importar_la_primera_vez(sm, tenant_id)
    antes = await _estado(sm, tenant_id)

    async with sm() as s:
        await reread_service.apply_reread(s, file_id, tenant_id, fresh_override=_summary("9000"))
        await s.commit()

    despues = await _estado(sm, tenant_id)
    assert despues != antes, "el escenario no cambia nada: los otros tests no probarían nada"


#: Punto de inyección → qué se escribió ANTES de que explote. El tercero es el
#: peligroso: corre la reconciliación entera (void + reimport) y falla después.
_PUNTOS = [
    ("_reread_master_entities", "el run del apply ya existe"),
    ("insert_confirmed_data", "los gastos viejos ya se anularon y el reimport no los repuso"),
    ("build_reread_summary", "gastos, productos, stock, movimientos y proveedor reescritos"),
]


@pytest.mark.parametrize(("romper", "que_ya_se_escribio"), _PUNTOS, ids=[p[0] for p in _PUNTOS])
async def test_un_fallo_a_mitad_no_deja_medio_estado(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    romper: str,
    que_ya_se_escribio: str,
) -> None:
    """El estado después del rollback tiene que ser IDÉNTICO al de antes."""
    file_id = await _importar_la_primera_vez(sm, tenant_id)
    antes = await _estado(sm, tenant_id)

    await _apply_con_fallo(sm, tenant_id, file_id, monkeypatch, romper=romper)

    despues = await _estado(sm, tenant_id)
    assert despues == antes, (
        f"fallo inyectado en {romper} ({que_ya_se_escribio}) dejó estado persistido.\n"
        f"antes:   {antes}\ndespués: {despues}"
    )
    # Aserción explícita del peor caso, además de la comparación completa: un
    # gasto anulado sin su reemplazo deja al tenant sin esa compra.
    assert not [g for g in despues["gastos"] if g[2]], (
        f"quedaron gastos anulados sin su reemplazo: {despues['gastos']}"
    )
