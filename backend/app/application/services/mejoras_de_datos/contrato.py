"""Contrato de una «mejora de datos».

Una mejora tiene una ``clase``:

- ``completa_vacios``: sólo llena un valor que está en NULL. Se aplica sola (la
  dispara el Dashboard) y se puede deshacer.
- ``cambia_valores``: pisa un valor existente. Nunca corre sola: vista previa y PIN.

Idempotencia sin tabla de «aplicadas»: una mejora ``completa_vacios`` termina con
cero candidatos (el detector es el ledger). Para que un «Deshacer» no se reaplique
solo, el servicio excluye de la corrida automática toda entidad que ya tenga una
fila de auditoría de la mejora (lápida): sólo el apply manual puede forzarla.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.persistence.models.repair import DataRepairRun


class Clase(StrEnum):
    COMPLETA_VACIOS = "completa_vacios"
    CAMBIA_VALORES = "cambia_valores"


@dataclass(frozen=True)
class Candidato:
    """Una entidad que la mejora puede corregir. ``id`` es el de la entidad."""

    id: uuid.UUID
    etiqueta: str
    detalle: dict[str, Any] = field(default_factory=dict)


@dataclass
class Deteccion:
    """Resultado de ``detectar``: lo corregible y lo que queda «a mano»."""

    candidatos: list[Candidato] = field(default_factory=list)
    #: entidad → motivo por el que la evidencia no alcanza (se informa, no se toca).
    a_mano: dict[str, str] = field(default_factory=dict)


@dataclass
class Reversion:
    revertidos: list[str] = field(default_factory=list)
    #: entidad → motivo por el que ya no está como la dejó la corrida (se reintenta).
    pendientes: dict[str, str] = field(default_factory=dict)

    @property
    def completa(self) -> bool:
        return not self.pendientes


class Mejora(ABC):
    """Una corrección aplicable a datos ya cargados. Un módulo por mejora."""

    #: Estable: forma parte del ``repair_type`` y del ``decision_type`` auditados.
    id: str
    clase: Clase
    titulo: str

    @abstractmethod
    async def detectar(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        ids: set[uuid.UUID] | None = None,
        *,
        bloquear: bool = False,
    ) -> Deteccion:
        """SELECT-only salvo ``bloquear`` (``FOR UPDATE`` bajo el lock del servicio).

        Evidencia estricta: ante la duda, no es candidato. ``ids`` acota la
        detección (así ``aplicar`` revalida lo que eligió la vista previa).
        """

    @abstractmethod
    async def aplicar(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        candidatos: list[Candidato],
        run: DataRepairRun,
    ) -> int:
        """Escribe por ORM (los listeners ven el cambio) y deja la auditoría por
        fila con ``servicio.registrar_cambio``. Devuelve cuántas filas cambió."""

    @abstractmethod
    async def revertir(
        self, session: AsyncSession, tenant_id: uuid.UUID, run: DataRepairRun
    ) -> Reversion:
        """Restaura el «antes» sólo de lo que sigue como lo dejó la corrida.

        Saltea lo que ya figura en ``run.details_json['revertidos']`` (un segundo
        «Deshacer» reintenta sólo los pendientes) y devuelve los ids de entidad
        revertidos ahora; el servicio los suma a ese registro.
        """
