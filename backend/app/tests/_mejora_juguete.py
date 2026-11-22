"""Mejora de juguete para probar el MECANISMO de mejoras de datos (no una real).

Completa ``Product.description`` cuando está en NULL. Vive sólo en tests: el registro
de producción arranca vacío. Una columna sin listeners de costeo/stock aísla lo que
se prueba (contrato, lápida, lock, tandas, revert) de la lógica de dominio.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.mejoras_de_datos import registro, servicio
from app.application.services.mejoras_de_datos.contrato import (
    Candidato,
    Clase,
    Deteccion,
    Mejora,
    Reversion,
)
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun

VALOR = "completado"


class MejoraJuguete(Mejora):
    id = "juguete"
    clase = Clase.COMPLETA_VACIOS
    titulo = "Completar descripciones"

    async def detectar(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        ids: set[uuid.UUID] | None = None,
        *,
        bloquear: bool = False,
    ) -> Deteccion:
        q = select(Product).where(
            Product.tenant_id == tenant_id,
            Product.is_active.is_(True),
            Product.description.is_(None),
        )
        if ids is not None:
            q = q.where(Product.id.in_(ids))
        q = q.order_by(Product.id)
        if bloquear:
            q = q.with_for_update()
        filas = (await session.execute(q)).scalars().all()
        return Deteccion(candidatos=[Candidato(id=p.id, etiqueta=p.name) for p in filas])

    async def aplicar(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        candidatos: list[Candidato],
        run: DataRepairRun,
    ) -> int:
        ids = {c.id for c in candidatos}
        productos = (
            await session.execute(
                select(Product).where(Product.tenant_id == tenant_id, Product.id.in_(ids))
            )
        ).scalars()
        n = 0
        for p in productos:
            await servicio.registrar_cambio(
                session, tenant_id, run, self, p.id,
                {"description": None}, {"description": VALOR}, None,
            )
            p.description = VALOR
            n += 1
        await session.flush()
        return n

    async def revertir(
        self, session: AsyncSession, tenant_id: uuid.UUID, run: DataRepairRun
    ) -> Reversion:
        ya = set((run.details_json or {}).get("revertidos") or [])
        filas = (
            await session.execute(
                select(DecisionAuditLog.decision_data).where(
                    DecisionAuditLog.tenant_id == tenant_id,
                    DecisionAuditLog.decision_type == f"MEJORA_DE_DATOS:{self.id}",
                )
            )
        ).scalars()
        res = Reversion()
        for data in filas:
            if data["run_id"] != str(run.id) or data["entidad_id"] in ya:
                continue
            p = await session.get(
                Product, uuid.UUID(data["entidad_id"]), with_for_update=True
            )
            if p is None or p.tenant_id != tenant_id or p.description != VALOR:
                res.pendientes[data["entidad_id"]] = "El producto cambió después de la corrida."
                continue
            p.description = None
            res.revertidos.append(data["entidad_id"])
        await session.flush()
        return res


class MejoraQueCambia(MejoraJuguete):
    id = "juguete_cambia"
    clase = Clase.CAMBIA_VALORES
    titulo = "Cambia valores"


@contextmanager
def registro_con(*mejoras: Mejora) -> Iterator[None]:
    """Reemplaza el registro por las mejoras dadas y lo restaura al salir."""
    previo: dict[str, Any] = dict(registro._REGISTRO)
    registro._REGISTRO.clear()
    try:
        for m in mejoras:
            registro.registrar(m)
        yield
    finally:
        registro._REGISTRO.clear()
        registro._REGISTRO.update(previo)
