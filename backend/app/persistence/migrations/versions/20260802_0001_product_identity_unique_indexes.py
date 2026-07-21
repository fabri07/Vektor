"""Unicidad de identidad de producto (Fase 5-B) — 3 índices únicos

Revision ID: 20260802_0001
Revises: 20260801_0001
Create Date: 2026-08-02

Contexto
--------
Cierra la Fase 5: hasta acá la unicidad de identidad la sostenía SOLO código
(``_find_identity_matches`` + ``product_identity_guard``), que es un
SELECT-antes-de-INSERT con ventana TOCTOU. Estos tres índices la mueven a la DB,
que es el único lugar donde dos transacciones concurrentes no pueden ganarse.

  uq_products_tenant_barcode_norm       parcial: (tenant_id, barcode_normalized)
  uq_products_tenant_sku_norm           parcial: (tenant_id, sku_normalized)
  uq_inventory_balances_tenant_product  total:   (tenant_id, product_id)

Los de ``products`` son PARCIALES: solo entre ACTIVOS y solo con la clave
presente. Un tenant puede tener N inactivos con el mismo SKU (historial de bajas)
y N activos sin SKU. El predicado es el ESPEJO EXACTO del ``postgresql_where``
declarado en ``Product.__table_args__``; si cambia uno, cambian los dos.

GATE OPERATIVO
--------------
Esta migración NO se puede aplicar a Neon antes de correr el dedup de F3
(``scripts/dedupe_products_by_name.py``) y verificar cero duplicados fuertes y
cero balances duplicados. El preflight de acá abajo lo hace cumplir sin depender
de que alguien se acuerde: con datos sucios ABORTA con el conteo y el comando a
correr, en vez de dejar un índice inválido a medio construir.

Por qué CONCURRENTLY
--------------------
Esto corre en el ``preDeployCommand`` de Railway contra Neon con tráfico vivo. Un
``CREATE UNIQUE INDEX`` común toma un lock que bloquea TODA escritura sobre
``products`` mientras dura el build. ``CONCURRENTLY`` no bloquea, a cambio de dos
cosas que hay que manejar (y se manejan): no puede correr dentro de una
transacción —de ahí el ``autocommit_block``— y si falla NO revierte, deja el
índice en estado ``INVALID``, que no impone nada pero ocupa espacio y hace fallar
el reintento por nombre duplicado. Por eso: se limpia el inválido ANTES de
empezar, y se limpia también si el CREATE falla.

El preflight NO elimina la carrera
----------------------------------
Entre el SELECT del preflight y el CREATE pueden entrar duplicados nuevos: el
preflight lee, no bloquea. Esa carrera la cierra PostgreSQL haciendo fallar el
CREATE, y la migración la maneja como cualquier otro fallo (dropea el inválido y
re-lanza). El preflight no está para garantizar unicidad —eso lo hace el índice—
sino para que el 99% de los casos falle temprano, con un diagnóstico accionable,
en vez de con un IntegrityError críptico a mitad de un deploy.
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "20260802_0001"
down_revision = "20260801_0001"
branch_labels = None
depends_on = None


class _IndexSpec:
    """Spec de un índice a crear.

    Clase pelada a propósito, NO ``@dataclass``: Alembic carga los módulos de
    migración con un loader que no los registra en ``sys.modules``, y
    ``dataclasses`` resuelve las anotaciones vía ``sys.modules[cls.__module__]``
    → ``AttributeError: 'NoneType' object has no attribute '__dict__'`` al
    importar la migración. Verificado contra Postgres real.
    """

    def __init__(
        self,
        name: str,
        table: str,
        columns: tuple[str, ...],
        predicate: str | None,  # None = índice TOTAL (sin WHERE)
    ) -> None:
        self.name = name
        self.table = table
        self.columns = columns
        self.predicate = predicate

    def create_sql(self) -> str:
        cols = ", ".join(self.columns)
        where = f" WHERE {self.predicate}" if self.predicate else ""
        return (
            f"CREATE UNIQUE INDEX CONCURRENTLY {self.name} "
            f"ON {self.table} ({cols}){where}"
        )


# Espejo EXACTO de los Index(...) del ORM (Product / InventoryBalance).
_INDEXES: tuple[_IndexSpec, ...] = (
    _IndexSpec(
        name="uq_products_tenant_barcode_norm",
        table="products",
        columns=("tenant_id", "barcode_normalized"),
        predicate="is_active AND barcode_normalized IS NOT NULL AND barcode_normalized <> ''",
    ),
    _IndexSpec(
        name="uq_products_tenant_sku_norm",
        table="products",
        columns=("tenant_id", "sku_normalized"),
        predicate="is_active AND sku_normalized IS NOT NULL AND sku_normalized <> ''",
    ),
    _IndexSpec(
        name="uq_inventory_balances_tenant_product",
        table="inventory_balances",
        columns=("tenant_id", "product_id"),
        predicate=None,
    ),
)

# ── Preflight ────────────────────────────────────────────────────────────────
# Los WHERE copian LITERALMENTE el predicado de cada índice. Un precheck más
# amplio que su índice bloquearía deploys por datos que el índice acepta sin
# problema (ej. normalizados vacíos, o duplicados entre inactivos): eso sería un
# falso positivo que frena un deploy legítimo, no una protección.
_PREFLIGHT: tuple[tuple[str, str], ...] = (
    (
        "SKU activo duplicado",
        """
        SELECT count(*) FROM (
            SELECT tenant_id, sku_normalized
            FROM products
            WHERE is_active AND sku_normalized IS NOT NULL AND sku_normalized <> ''
            GROUP BY tenant_id, sku_normalized HAVING count(*) > 1
        ) d
        """,
    ),
    (
        "código de barras activo duplicado",
        """
        SELECT count(*) FROM (
            SELECT tenant_id, barcode_normalized
            FROM products
            WHERE is_active AND barcode_normalized IS NOT NULL AND barcode_normalized <> ''
            GROUP BY tenant_id, barcode_normalized HAVING count(*) > 1
        ) d
        """,
    ),
    (
        "balance duplicado por (tenant_id, product_id)",
        """
        SELECT count(*) FROM (
            SELECT tenant_id, product_id
            FROM inventory_balances
            GROUP BY tenant_id, product_id HAVING count(*) > 1
        ) d
        """,
    ),
)

_EXISTING_INDEX_SQL = sa.text(
    """
    SELECT i.indisunique AS is_unique,
           i.indisvalid  AS is_valid,
           pg_get_expr(i.indpred, i.indrelid) AS predicate,
           (SELECT string_agg(a.attname, ',' ORDER BY k.ord)
              FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
              JOIN pg_attribute a
                ON a.attrelid = i.indrelid AND a.attnum = k.attnum) AS cols
      FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relname = :name AND n.nspname = current_schema()
    """
)


def _canonical(predicate: str | None) -> str:
    """Normaliza un predicado para comparar el nuestro con el que renderiza PG.

    PostgreSQL no guarda el texto original: lo reconstruye desde el árbol, con
    paréntesis y casts propios. Nuestro ``barcode_normalized <> ''`` vuelve como
    ``(... AND (barcode_normalized <> ''::text))``. Comparar crudo daría
    "incompatible" para un predicado idéntico. Se comparan sin paréntesis, sin
    casts ``::text`` y en minúscula; cualquier diferencia REAL (otra columna, otro
    operador, un AND de más) sobrevive a esta normalización.

    CUIDADO AL AGREGAR PREDICADOS NUEVOS: borrar los paréntesis también borra el
    AGRUPAMIENTO, así que ``(a AND b) OR c`` y ``a AND (b OR c)`` canonizan igual.
    Hoy no es un agujero porque los tres predicados son conjunciones PURAS, y
    reagrupar un AND no cambia el significado —lo único que esta función acepta de
    más es una reescritura equivalente—. Un predicado con ``OR`` rompe esa
    propiedad: ahí un índice ajeno con los mismos tokens y otro agrupamiento
    pasaría como "compatible" y la migración lo daría por bueno sin crear el suyo.
    Si aparece un ``OR``, comparar de otra forma (o parentizar explícitamente).
    """
    if predicate is None:
        return ""
    s = predicate.lower().replace("::text", "")
    s = s.replace("(", " ").replace(")", " ")
    return re.sub(r"\s+", " ", s).strip()


def _run_preflight(conn: sa.Connection) -> None:
    sucios = [
        (etiqueta, total)
        for etiqueta, sql in _PREFLIGHT
        if (total := conn.execute(sa.text(sql)).scalar_one()) > 0
    ]
    if not sucios:
        return
    detalle = "\n".join(f"  - {etiqueta}: {total} grupo(s)" for etiqueta, total in sucios)
    raise RuntimeError(
        "F5-B ABORTADA: la base todavía tiene identidades duplicadas.\n"
        f"{detalle}\n\n"
        "Los índices únicos no se pueden crear sobre datos sucios. Corré el dedup "
        "de F3 y volvé a deployar:\n"
        "  python scripts/dedupe_products_by_name.py --all-active --out plan.csv\n"
        "  # revisar plan.csv, después por tenant:\n"
        "  python scripts/dedupe_products_by_name.py --tenant <uuid> --apply "
        "--source-run-id <run_uuid>\n"
    )


def _drop_concurrently(conn: sa.Connection, name: str) -> None:
    conn.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))


def _ensure_index(conn: sa.Connection, spec: _IndexSpec) -> None:
    """Crea el índice si falta; si ya está, lo valida en vez de asumir."""
    row = conn.execute(_EXISTING_INDEX_SQL, {"name": spec.name}).mappings().first()

    if row is not None and not row["is_valid"]:
        # Sobra de una corrida anterior de CONCURRENTLY que falló. No impone nada:
        # se limpia y se rehace. Concurrently también para no lockear.
        _drop_concurrently(conn, spec.name)
        row = None

    if row is not None:
        esperado_cols = ",".join(spec.columns)
        problemas = []
        if not row["is_unique"]:
            problemas.append("no es UNIQUE")
        if row["cols"] != esperado_cols:
            problemas.append(f"columnas {row['cols']!r} (esperado {esperado_cols!r})")
        if _canonical(row["predicate"]) != _canonical(spec.predicate):
            problemas.append(
                f"predicado {row['predicate']!r} (esperado {spec.predicate!r})"
            )
        if problemas:
            raise RuntimeError(
                f"F5-B ABORTADA: ya existe un índice llamado {spec.name!r} que NO es "
                f"el de esta migración: {'; '.join(problemas)}.\n"
                "No se toca: dropearlo a ciegas podría borrar un índice que alguien "
                "creó a mano por otra razón. Resolvelo y volvé a deployar."
            )
        return  # válido y compatible → idempotente, nada que hacer

    try:
        conn.execute(sa.text(spec.create_sql()))
    except Exception:
        # CONCURRENTLY que falla NO revierte: deja el índice INVALID y el reintento
        # chocaría por nombre duplicado. Se limpia acá para que el próximo deploy
        # arranque limpio, y se re-lanza el error original sin enmascararlo.
        _drop_concurrently(conn, spec.name)
        raise

    verif = conn.execute(_EXISTING_INDEX_SQL, {"name": spec.name}).mappings().first()
    if verif is None or not verif["is_unique"] or not verif["is_valid"]:
        raise RuntimeError(
            f"F5-B ABORTADA: {spec.name!r} no quedó UNIQUE+VALID tras crearse "
            f"({verif and dict(verif)}). No se sigue: sin ese índice la unicidad "
            "vuelve a depender solo del código."
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (tests) obtiene los tres índices por create_all desde el ORM.
        return

    _run_preflight(bind)

    # CONCURRENTLY no puede correr dentro de una transacción.
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        for spec in _INDEXES:
            _ensure_index(conn, spec)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        for spec in reversed(_INDEXES):
            _drop_concurrently(conn, spec.name)
