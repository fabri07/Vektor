"""Dos `alembic upgrade head` al mismo tiempo tienen que terminar los dos bien.

Railway despliega `vektor-api` y `vektor-worker` en paralelo y los dos corren
`scripts/migrate.sh`. El 2026-09-24 los dos vieron que `idempotency_records` no
existía —los guards de E8c comprueban presencia, no son atómicos—, los dos la
crearon, y el deploy del worker abortó con `UniqueViolation` sobre `pg_type`.

El arreglo es un advisory lock de transacción en `migrations/env.py`: el segundo
proceso espera, lee la versión ya commiteada y no hace nada. Este test lanza
varios upgrades a la vez sobre las migraciones reales del POS.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
import sqlalchemy as sa

from app.tests.integration.test_migraciones_idempotentes_pg import (
    BACKEND,
    TEST_PG_DSN,
    _alembic,
    _head,
    base_limpia,  # noqa: F401 — fixture
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

#: Antes de las dos migraciones que chocaron en producción.
ANTES_DEL_POS = "20260919_0001"
PROCESOS = 4
RONDAS = 3


def _upgrade_en_paralelo(base: str) -> list[subprocess.CompletedProcess[str]]:
    entorno = {**os.environ, "DATABASE_URL": base, "DATABASE_URL_SYNC": base}
    procesos = [
        subprocess.Popen(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND,
            env=entorno,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(PROCESOS)
    ]
    resultados = []
    for p in procesos:
        out, err = p.communicate(timeout=300)
        resultados.append(subprocess.CompletedProcess(p.args, p.returncode, out, err))
    return resultados


def test_upgrades_simultaneos_no_se_pisan(base_limpia: str) -> None:  # noqa: F811
    primera = _alembic(base_limpia, "upgrade", "head")
    assert primera.returncode == 0, primera.stderr[-3000:]
    head = _head()

    for ronda in range(RONDAS):
        bajada = _alembic(base_limpia, "downgrade", ANTES_DEL_POS)
        assert bajada.returncode == 0, bajada.stderr[-3000:]

        resultados = _upgrade_en_paralelo(base_limpia)
        fallidos = [r for r in resultados if r.returncode != 0]
        assert not fallidos, (
            f"ronda {ronda}: {len(fallidos)} de {PROCESOS} upgrades simultáneos fallaron — "
            f"es el UniqueViolation del 2026-09-24:\n{fallidos[0].stderr[-2500:]}"
        )

        motor = sa.create_engine(base_limpia)
        with motor.connect() as conn:
            version = conn.execute(sa.text("select version_num from alembic_version")).scalar()
            tablas = set(sa.inspect(conn).get_table_names())
        motor.dispose()
        assert version == head
        assert {"idempotency_records", "pos_operations", "pos_tenders"} <= tablas
