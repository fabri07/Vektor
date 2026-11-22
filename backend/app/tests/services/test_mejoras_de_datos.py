"""Mecanismo de «mejoras de datos»: contrato, lápida, tandas, revert y tarea.

Se prueba con una mejora de juguete (``app/tests/_mejora_juguete.py``): lo que se
protege es el MECANISMO — idempotencia, no pisar, aislamiento de tenant, que un
«Deshacer» no se reaplique solo, y que ``aplicar`` revalide bajo lock.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.mejoras_de_datos import servicio
from app.jobs.mejoras_de_datos import procesar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.tests._mejora_juguete import (
    VALOR,
    MejoraJuguete,
    MejoraQueCambia,
    registro_con,
)

pytestmark = pytest.mark.usefixtures("mock_score_trigger")


@pytest.fixture(autouse=True)
def _registro() -> Iterator[None]:
    with registro_con(MejoraJuguete(), MejoraQueCambia()):
        yield


async def _producto(
    db: AsyncSession, tenant: Tenant, nombre: str, descripcion: str | None = None
) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name=nombre,
        sale_price_ars=Decimal("1000"),
        stock_units=1,
        provenance="REAL",
        description=descripcion,
    )
    db.add(p)
    await db.flush()
    return p


async def _cuenta(db: AsyncSession, modelo: Any, *cond: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(modelo).where(*cond)) or 0)


async def test_completa_vacios_no_pisa_y_es_idempotente(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    a = await _producto(db_session, sample_tenant, "A")
    b = await _producto(db_session, sample_tenant, "B")
    con_valor = await _producto(db_session, sample_tenant, "C", "mío")
    tid = sample_tenant.tenant_id

    r = await servicio.aplicar(db_session, tid, "juguete", automatica=True)

    assert r.aplicadas == 2 and r.restantes == 0 and r.run_id is not None
    await db_session.refresh(a)
    await db_session.refresh(b)
    await db_session.refresh(con_valor)
    assert (a.description, b.description) == (VALOR, VALOR)
    assert con_valor.description == "mío"  # nunca pisa un valor existente
    run = await db_session.get(DataRepairRun, r.run_id)
    assert run is not None
    assert (run.repair_type, run.status, run.dry_run) == ("MEJORA:juguete", "APPLIED", False)
    assert (
        await _cuenta(
            db_session,
            DecisionAuditLog,
            DecisionAuditLog.decision_type == "MEJORA_DE_DATOS:juguete",
        )
        == 2
    )

    # Segunda corrida: el detector es el ledger → cero candidatos, ni run nuevo.
    de_nuevo = await servicio.aplicar(db_session, tid, "juguete", automatica=True)
    assert de_nuevo.aplicadas == 0 and de_nuevo.run_id is None
    assert await _cuenta(db_session, DataRepairRun, DataRepairRun.tenant_id == tid) == 1


async def test_no_toca_a_otro_tenant(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    mio = await _producto(db_session, sample_tenant, "Mío")
    ajeno = await _producto(db_session, second_tenant, "Ajeno")

    await servicio.aplicar(db_session, sample_tenant.tenant_id, "juguete", automatica=True)

    await db_session.refresh(mio)
    await db_session.refresh(ajeno)
    assert mio.description == VALOR
    assert ajeno.description is None
    runs_ajenos = await _cuenta(
        db_session, DataRepairRun, DataRepairRun.tenant_id == second_tenant.tenant_id
    )
    assert runs_ajenos == 0


async def test_aplica_por_tandas(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    for i in range(3):
        await _producto(db_session, sample_tenant, f"P{i}")
    tid = sample_tenant.tenant_id

    uno = await servicio.aplicar(db_session, tid, "juguete", automatica=True, limite=1)
    assert (uno.aplicadas, uno.restantes) == (1, 2)
    resto = await servicio.aplicar(db_session, tid, "juguete", automatica=True, limite=10)
    assert (resto.aplicadas, resto.restantes) == (2, 0)


async def test_aplicar_revalida_los_ids_de_la_vista_previa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    viejo = await _producto(db_session, sample_tenant, "Ya completado")
    nuevo = await _producto(db_session, sample_tenant, "Pendiente")
    viejo_id, nuevo_id = viejo.id, nuevo.id
    # La vista previa vio los dos; entre medio alguien completó el primero a mano.
    viejo.description = "a mano"
    await db_session.flush()

    r = await servicio.aplicar(
        db_session, sample_tenant.tenant_id, "juguete", {viejo_id, nuevo_id}
    )

    assert r.aplicadas == 1
    await db_session.refresh(viejo)
    assert viejo.description == "a mano"  # no se confió en la vista previa


async def test_revertir_restaura_y_el_automatico_no_lo_reaplica(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant, "A")
    tid = sample_tenant.tenant_id
    r = await servicio.aplicar(db_session, tid, "juguete", automatica=True)
    assert r.run_id is not None

    rev = await servicio.revertir(db_session, tid, r.run_id)

    assert rev.completa and rev.revertidos == [str(p.id)]
    await db_session.refresh(p)
    assert p.description is None
    run = await db_session.get(DataRepairRun, r.run_id)
    assert run is not None and run.status == "REVERTED"

    # El detector la vuelve a ver, pero la lápida la excluye de lo automático...
    assert await servicio.hay_candidatos_automaticos(db_session, tid) is False
    sola = await servicio.aplicar(db_session, tid, "juguete", automatica=True)
    assert sola.aplicadas == 0
    await db_session.refresh(p)
    assert p.description is None
    # ...y sólo el apply manual puede forzarla.
    manual = await servicio.aplicar(db_session, tid, "juguete")
    assert manual.aplicadas == 1


async def test_revertir_una_fila_editada_queda_pendiente_y_no_se_repite(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    a = await _producto(db_session, sample_tenant, "A")
    b = await _producto(db_session, sample_tenant, "B")
    tid = sample_tenant.tenant_id
    r = await servicio.aplicar(db_session, tid, "juguete", automatica=True)
    assert r.run_id is not None
    b.description = "editado después"
    await db_session.flush()

    rev = await servicio.revertir(db_session, tid, r.run_id)

    assert rev.revertidos == [str(a.id)]
    assert list(rev.pendientes) == [str(b.id)] and not rev.completa
    await db_session.refresh(b)
    assert b.description == "editado después"  # no se pisa lo que cambió el usuario
    run = await db_session.get(DataRepairRun, r.run_id)
    assert run is not None and run.status == "APPLIED"  # incompleta: no queda REVERTED

    de_nuevo = await servicio.revertir(db_session, tid, r.run_id)
    assert de_nuevo.revertidos == []  # lo ya revertido no se reintenta


async def test_una_mejora_que_cambia_valores_nunca_corre_sola(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _producto(db_session, sample_tenant, "A")
    with pytest.raises(servicio.ClaseIncorrectaError):
        await servicio.aplicar(
            db_session, sample_tenant.tenant_id, "juguete_cambia", automatica=True
        )
    # A mano sí, y el automático ni la mira.
    assert await servicio.hay_candidatos_automaticos(db_session, sample_tenant.tenant_id)


async def test_revertir_una_corrida_de_otro_tenant_es_invalido(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    await _producto(db_session, sample_tenant, "A")
    r = await servicio.aplicar(db_session, sample_tenant.tenant_id, "juguete", automatica=True)
    assert r.run_id is not None
    with pytest.raises(servicio.CorridaInvalidaError):
        await servicio.revertir(db_session, second_tenant.tenant_id, r.run_id)


async def test_mejora_desconocida(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    with pytest.raises(servicio.MejoraDesconocidaError):
        await servicio.aplicar(db_session, sample_tenant.tenant_id, "no_existe")


async def test_analizar_es_solo_lectura_y_muestra_lo_aplicado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _producto(db_session, sample_tenant, "A")
    tid = sample_tenant.tenant_id

    antes = await servicio.analizar(db_session, tid)
    assert [(p.mejora_id, p.cantidad) for p in antes.pendientes] == [
        ("juguete", 1),
        ("juguete_cambia", 1),
    ]
    assert antes.aplicadas == []
    assert await _cuenta(db_session, DataRepairRun, DataRepairRun.tenant_id == tid) == 0

    await servicio.aplicar(db_session, tid, "juguete", automatica=True)
    despues = await servicio.analizar(db_session, tid)
    assert [a.mejora_id for a in despues.aplicadas] == ["juguete"]
    assert despues.aplicadas[0].revertible and despues.aplicadas[0].aplicadas == 1


# ── Tarea ────────────────────────────────────────────────────────────────────


def _fabrica(db: AsyncSession) -> Any:
    @asynccontextmanager
    async def _abrir() -> AsyncIterator[AsyncSession]:
        yield db

    return cast(Any, _abrir)


async def test_procesar_aplica_solo_las_que_completan_vacios(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant, "A")

    totales = await procesar(_fabrica(db_session), sample_tenant.tenant_id)

    assert totales == {"juguete": 1}  # la que cambia valores no corre sola
    await db_session.refresh(p)
    assert p.description == VALOR
    assert await procesar(_fabrica(db_session), sample_tenant.tenant_id) == {"juguete": 0}


async def test_procesar_no_escribe_en_un_tenant_de_solo_lectura(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant, "A")
    sample_tenant.status = "READ_ONLY"
    await db_session.flush()

    assert await procesar(_fabrica(db_session), sample_tenant.tenant_id) == {}
    await db_session.refresh(p)
    assert p.description is None


async def test_procesar_una_mejora_rota_no_corta_a_las_otras(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Rota(MejoraJuguete):
        id = "rota"

        async def aplicar(self, *a: Any, **k: Any) -> int:
            raise RuntimeError("falla")

    p = await _producto(db_session, sample_tenant, "A")
    with registro_con(Rota(), MejoraJuguete()):
        totales = await procesar(_fabrica(db_session), sample_tenant.tenant_id)

    assert totales["rota"] == 0
    assert totales["juguete"] == 1
    await db_session.refresh(p)
    assert p.description == VALOR
