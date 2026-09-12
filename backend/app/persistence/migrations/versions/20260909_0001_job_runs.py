"""``job_runs`` — traza durable de los jobs periódicos (E5/H16)

Revision ID: 20260909_0001
Revises: 20260906_0001
Create Date: 2026-09-09

Contexto
--------
Migración ADDITIVE — tabla nueva, no toca nada existente.

H16 pedía verificar "qué corre realmente" de los jobs periódicos, y el código no
lo puede demostrar: ``jobs.sweep_stale_reread_runs`` —el barrido que recupera
relecturas colgadas, programado cada 10 minutos— sólo escribía en el log. Sin un
rastro en la base, "Beat lo está publicando y un worker lo consume" no se puede
comprobar con una consulta, sólo buscando en los logs del servicio, que es
exactamente el tipo de verificación que no se puede automatizar ni auditar
después.

Por qué una tabla nueva y no ``user_activity_events``
-----------------------------------------------------
El registro de jobs existente (``track_job_event``) escribe ahí, y esa tabla
tiene ``tenant_id`` NOT NULL con FK a ``tenants``. Sirve para un job que trabaja
sobre UN tenant; no para uno global, que es el caso del barrido. Su rama
``tenant_id=None`` cae al UUID cero, que no existe en ``tenants``: habría fallado
con violación de FK la primera vez que se usara.

Orden de despliegue
-------------------
El único escritor es el worker (``jobs.sweep_stale_reread_runs``), y las
migraciones corren en el ``preDeployCommand`` de **``vektor-api``**. Railway
redespliega los servicios EN PARALELO y sin orden garantizado, así que el worker
puede arrancar antes de que esta tabla exista.

Consecuencia declarada, y por qué no se blinda en código: si el barrido corre sin
la tabla, su ``INSERT`` falla, la transacción se deshace y **la task falla
ruidosamente sin cerrar ningún run** — no corrompe nada, y Beat la vuelve a
publicar a los 10 minutos, así que se recupera sola en cuanto la migración
aterriza. Tragarse ese error para "no molestar" sería peor: dejaría el barrido
funcionando sin traza, que es exactamente el estado que esta tabla vino a
terminar. La ventana es de minutos y el único costo es housekeeping diferido.

Sin FK, sin tenant y sin datos personales: nombre del job, inicio, duración,
resultado y contadores agregados. Se escribe una fila por ejecución **también
cuando no encuentra nada que barrer** — sin la ejecución vacía, un Beat caído y
un sistema sano se ven exactamente igual desde la base.

Idempotente (E8c)
-----------------
``upgrade()`` saltea lo que ya existe y ``downgrade()`` sólo borra lo que está.
El ``preDeployCommand`` de Railway corre ``alembic upgrade head`` en cada deploy,
y un esquema que quedó por delante de ``alembic_version`` —pasó el 2026-09-12—
hace fallar el deploy entero. Misma convención que ``20260806_0001``, que ya lo
documenta: "el ``preDeployCommand`` puede correr dos veces".

Límite declarado: comprueba PRESENCIA, no forma. Una tabla que exista con otras
columnas se saltea igual; detectar eso pide comparar el esquema entero y es otro
problema.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "20260909_0001"
down_revision = "20260906_0001"
branch_labels = None
depends_on = None


def _tablas() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indices(tabla: str) -> set[str]:
    # Vacío si la tabla no está: el `downgrade` pregunta por los índices de una
    # tabla que quizá ya no exista, y `get_indexes` sobre eso levanta
    # `NoSuchTableError`.
    insp = sa.inspect(op.get_bind())
    if tabla not in insp.get_table_names():
        return set()
    return {ix["name"] for ix in insp.get_indexes(tabla)}


def upgrade() -> None:
    if "job_runs" not in _tablas():
        op.create_table(
            "job_runs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("job_name", sa.String(length=100), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("counters", JSONB(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
        )
    # Índice compuesto: la consulta operativa es siempre "últimas corridas de ESTE
    # job", nunca un scan por fecha global.
    if "ix_job_runs_job_name_started_at" not in _indices("job_runs"):
        op.create_index(
            "ix_job_runs_job_name_started_at",
            "job_runs",
            ["job_name", "started_at"],
        )


def downgrade() -> None:
    if "ix_job_runs_job_name_started_at" in _indices("job_runs"):
        op.drop_index("ix_job_runs_job_name_started_at", table_name="job_runs")
    if "job_runs" in _tablas():
        op.drop_table("job_runs")
