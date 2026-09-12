"""E8c — el `preDeployCommand` puede correr sobre un esquema ya adelantado.

El 2026-09-12 un deploy abortó con ``DuplicateColumn``: ``alembic_version`` decía
``20260903_0001`` y la columna que agrega ``20260906_0001`` ya existía. El
fail-safe funcionó —``set -e`` cortó y la versión vieja siguió sirviendo— pero el
deploy no salía hasta que alguien tocara la base a mano.

La convención del repo ya decía qué hacer: ``20260806_0001`` documenta que "el
``preDeployCommand`` puede correr dos veces" y guarda cada creación detrás de un
chequeo. Las 7 migraciones de este programa no la habían seguido.

Lo que prueba este test es el escenario REAL, no "correr upgrade dos veces"
—alembic saltearía por versión y no ejecutaría nada—: se aplica la cadena, se
retrocede el STAMP sin tocar el esquema (que es exactamente la deriva observada)
y se vuelve a aplicar. Antes del arreglo, esa segunda pasada explota.

Corre contra PostgreSQL real a propósito: SQLite no distingue la mitad de estos
objetos y el defecto que se arregla es de PostgreSQL.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

#: La revisión anterior a las 7 de este programa. Retroceder el stamp hasta acá
#: reproduce la deriva: el esquema queda por delante de lo que alembic declara.
ANTES_DEL_PROGRAMA = "20260903_0001"

BACKEND = pathlib.Path(__file__).resolve().parents[3]


def _sync_dsn(dsn: str) -> str:
    """El DSN de alembic (psycopg2), desde el de los tests (asyncpg)."""
    return dsn.replace("+asyncpg", "")


def _alembic(base: str, *args: str) -> subprocess.CompletedProcess[str]:
    entorno = {**os.environ, "DATABASE_URL": base, "DATABASE_URL_SYNC": base}
    return subprocess.run(
        [str(BACKEND / ".venv" / "bin" / "alembic"), *args],
        cwd=BACKEND,
        env=entorno,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def base_limpia() -> Iterator[str]:
    """Una base propia por corrida: el test aplica la cadena ENTERA."""
    assert TEST_PG_DSN is not None
    admin = _sync_dsn(TEST_PG_DSN).rsplit("/", 1)[0] + "/postgres"
    nombre = f"vektor_idem_{uuid.uuid4().hex[:8]}"
    motor = sa.create_engine(admin, isolation_level="AUTOCOMMIT")
    with motor.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{nombre}"'))
    motor.dispose()
    base = _sync_dsn(TEST_PG_DSN).rsplit("/", 1)[0] + f"/{nombre}"
    try:
        yield base
    finally:
        motor = sa.create_engine(admin, isolation_level="AUTOCOMMIT")
        with motor.connect() as conn:
            conn.execute(
                sa.text(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    f"where datname = '{nombre}'"
                )
            )
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{nombre}"'))
        motor.dispose()


def test_la_cadena_se_reaplica_sobre_un_esquema_ya_adelantado(base_limpia: str) -> None:
    primera = _alembic(base_limpia, "upgrade", "head")
    assert primera.returncode == 0, primera.stderr[-3000:]

    # La deriva, reproducida: el esquema queda intacto y el stamp retrocede. Es lo
    # que se encontró en Neon, y lo que `upgrade` vuelve a recorrer en el próximo
    # deploy.
    motor = sa.create_engine(base_limpia)
    with motor.begin() as conn:
        conn.execute(sa.text("update alembic_version set version_num = :v"),
                     {"v": ANTES_DEL_PROGRAMA})
    motor.dispose()

    segunda = _alembic(base_limpia, "upgrade", "head")
    assert segunda.returncode == 0, (
        "la re-aplicación sobre un esquema adelantado falló — es el "
        f"DuplicateColumn del 2026-09-12:\n{segunda.stderr[-3000:]}"
    )

    # Y terminó donde corresponde, no a mitad de camino.
    motor = sa.create_engine(base_limpia)
    with motor.connect() as conn:
        version = conn.execute(sa.text("select version_num from alembic_version")).scalar_one()
        columnas = {
            c["name"] for c in sa.inspect(conn).get_columns("uploaded_files")
        }
    motor.dispose()
    assert version == "20260910_0005"
    assert "parse_attempt_id" in columnas


def test_el_downgrade_no_explota_sobre_lo_que_ya_no_esta(base_limpia: str) -> None:
    """La reversa también: `drop` sólo lo que existe.

    Un `downgrade` sobre un esquema al que ya le falta algo tiene que terminar,
    no abortar a mitad y dejar la cadena trabada.
    """
    assert _alembic(base_limpia, "upgrade", "head").returncode == 0

    motor = sa.create_engine(base_limpia)
    with motor.begin() as conn:
        conn.execute(sa.text("alter table uploaded_files drop column parse_attempt_id"))
        conn.execute(sa.text("drop table if exists job_runs"))
    motor.dispose()

    vuelta = _alembic(base_limpia, "downgrade", ANTES_DEL_PROGRAMA)
    assert vuelta.returncode == 0, vuelta.stderr[-3000:]
