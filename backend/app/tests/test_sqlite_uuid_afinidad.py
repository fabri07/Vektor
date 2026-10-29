"""Un UUID cuyo hex parece un número no puede volver como número (tests, SQLite).

Regresión del flaky `'float' object has no attribute 'replace'`: ver el hook de
`postgresql.UUID` en `conftest.py`.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

_TABLA = sa.Table(
    "t_uuid_afinidad",
    sa.MetaData(),
    sa.Column("id", PG_UUID(as_uuid=True), primary_key=True),
)

# Hex de sólo dígitos y de dígitos con una `e`: los que SQLite leía como número.
_PARECIDOS_A_NUMEROS = [
    uuid.UUID(hex="1" * 32),
    uuid.UUID(hex="1" * 31 + "e"),
    uuid.UUID(hex="4742801015254051234567890123" + "e" + "331"),
]


async def test_un_uuid_que_parece_numero_vuelve_igual(db_session: AsyncSession) -> None:
    await db_session.run_sync(lambda s: _TABLA.create(s.connection(), checkfirst=True))
    for valor in _PARECIDOS_A_NUMEROS:
        await db_session.execute(sa.insert(_TABLA).values(id=valor))
    leidos = (await db_session.execute(sa.select(_TABLA.c.id))).scalars().all()
    assert sorted(leidos) == sorted(_PARECIDOS_A_NUMEROS)
