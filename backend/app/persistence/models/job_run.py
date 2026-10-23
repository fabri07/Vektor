"""``JobRun`` — traza durable de un job PERIÓDICO, sin tenant.

Por qué no alcanzaba ``track_job_event``
---------------------------------------
El registro de jobs que ya existía escribe en ``user_activity_events``, cuya
``tenant_id`` es NOT NULL y tiene FK a ``tenants``. Sirve para un job que trabaja
sobre UN tenant (recalcular un score, parsear un archivo), y no para uno global:
``jobs.sweep_stale_reread_runs`` barre los runs colgados de TODOS los tenants y
la mayoría de sus ejecuciones no tocan ninguno. Su rama ``tenant_id=None`` cae al
UUID cero, que no existe en ``tenants`` — habría fallado con violación de FK la
primera vez que se usara.

Qué habilita
------------
Que "¿el barrido está corriendo?" sea una consulta y no una búsqueda en los logs.
Antes de esto, ``sweep_stale_reread_runs`` sólo logueaba: no había forma de
demostrar desde la base que Beat lo publicó y que un worker lo consumió, que es
justamente la verificación operativa pendiente del programa (H16).

**Se registra TODA ejecución, incluso la que no encuentra nada.** Es lo que
distingue "el barrido corrió y no había nada colgado" de "el barrido no corrió":
sin la ejecución vacía, un Beat caído y un sistema sano se ven igual.

Insert-only y sin datos personales: nombre del job, cuándo empezó, cuánto tardó,
si salió bien y contadores agregados.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base


class JobRun(Base):
    __tablename__ = "job_runs"

    # Índice COMPUESTO y no uno por columna: la única consulta es "últimas
    # corridas de ESTE job". Declararlo acá y no con `index=True` en cada columna
    # mantiene el modelo alineado con lo que crea la migración — dos índices que
    # el ORM inventa y la migración no son una divergencia que aparece recién
    # cuando alguien corre `create_all` contra una base migrada.
    __table_args__ = (Index("ix_job_runs_job_name_started_at", "job_name", "started_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: Nombre de la task de Celery, tal cual (``jobs.sweep_stale_reread_runs``).
    job_name: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Qué hizo la corrida. Agregados, nunca filas ni identificadores de negocio.
    counters: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)
    #: Texto del error cuando falló. Truncado por el escritor.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug
        return f"<JobRun {self.job_name} success={self.success} {self.duration_ms}ms>"
