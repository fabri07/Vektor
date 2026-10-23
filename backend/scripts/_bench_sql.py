"""Instrumentación compartida de los benchmarks: statements por forma + tiempo por fase.

Nace de ``bench_reread_apply.py`` (Fase 1.1 de la relectura), que fue el primer lugar
donde hizo falta distinguir "mucho trabajo legítimo" de un N+1. Se extrajo acá sin
cambios de comportamiento porque el bench del confirm necesita exactamente lo mismo y
dos copias divergirían en la primera corrección: la conclusión de un bench depende de
cómo agrupa el SQL, así que dos agrupadores distintos darían dos diagnósticos distintos
sobre la misma corrida.

**El conteo de statements es lo que importa, no los segundos.** Contra Postgres local
la latencia por statement es ~0,1 ms y contra Neon ~30-50 ms: los segundos locales son
un piso, no una estimación. Un N+1, en cambio, se ve igual en las dos.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any


class SqlProfile:
    """Cuenta statements y tiempo agrupando por FORMA de SQL, no por texto exacto.

    La forma (``INSERT INTO data_repair_items``, ``SELECT products``, ``advisory
    lock``) es lo que distingue "mucho trabajo legítimo" de un N+1: 405 llamadas a
    ``pg_advisory_lock`` para 405 movimientos es un N+1; 2.563 INSERT para 2.563
    registros no lo es.

    ``SAVEPOINT``/``RELEASE``/``ROLLBACK TO`` se cuentan como formas propias: son el
    peaje de ``guarded_savepoint`` y su cantidad es la señal directa de si un
    get-or-create corre por fila o por lote.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)
        self.seconds: dict[str, float] = defaultdict(float)
        self.enabled = False

    def shape(self, sql: str) -> str:
        s = " ".join(str(sql).split())[:400]
        low = s.lower()
        if "advisory" in low:
            return "pg_advisory_lock (lock)"
        # Las transacciones anidadas viajan como statements sueltos y sin tabla; sin
        # esta rama caerían al `s[:60]` final y cada SAVEPOINT con nombre distinto
        # ("SAVEPOINT sa_savepoint_3") sería una forma propia, volviendo invisible
        # justo el número que se quiere vigilar.
        for verbo, etiqueta in (
            ("savepoint", "SAVEPOINT"),
            ("release savepoint", "RELEASE SAVEPOINT"),
            ("rollback to savepoint", "ROLLBACK TO SAVEPOINT"),
        ):
            if low.startswith(verbo):
                return etiqueta
        for verb, pat in (
            ("INSERT", r"insert\s+into\s+([a-z_\.\"]+)"),
            ("UPDATE", r"update\s+([a-z_\.\"]+)"),
            ("DELETE", r"delete\s+from\s+([a-z_\.\"]+)"),
            ("SELECT", r"\bfrom\s+([a-z_\.\"]+)"),
        ):
            m = re.search(pat, low)
            if low.startswith(verb.lower()) and m:
                return f"{verb} {m.group(1).strip(chr(34))}"
        return s[:60]

    def record(self, sql: str, elapsed: float) -> None:
        if not self.enabled:
            return
        k = self.shape(sql)
        self.counts[k] += 1
        self.seconds[k] += elapsed

    def reset(self) -> None:
        self.counts.clear()
        self.seconds.clear()

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def report(self, title: str, limit: int = 25) -> None:
        p(f"{title} — {self.total} statements")
        print(f"  {'statements':>10}  {'seg':>8}  forma")
        print(f"  {'-' * 10}  {'-' * 8}  {'-' * 50}")
        for k in sorted(self.counts, key=lambda x: -self.counts[x])[:limit]:
            print(f"  {self.counts[k]:>10}  {self.seconds[k]:>8.2f}  {k}")


def p(title: str) -> None:
    """Mismo formato que ``asteria_dryrun_bloque7.p``: los dos benchmarks imprimen a
    la misma consola y una diferencia de encabezado se lee como si fueran dos
    herramientas distintas."""
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def attach(engine: Any, profile: SqlProfile) -> None:
    """Engancha el contador al engine. Idempotente por engine nuevo."""
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn: Any, cursor: Any, statement: Any, *rest: Any) -> None:
        conn.info["_bench_t0"] = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after(conn: Any, cursor: Any, statement: Any, *rest: Any) -> None:
        profile.record(statement, time.perf_counter() - conn.info.get("_bench_t0", 0.0))


class PhaseTimer:
    """Envuelve funciones del servicio para medirlas sin instrumentar el código real.

    Se monkeypatchea en el módulo que las IMPORTÓ (``reread_service`` hace
    ``from ... import void_movement``, así que el patch va ahí, no en el origen).
    """

    def __init__(self, profile: SqlProfile) -> None:
        self.profile = profile
        self.calls: dict[str, int] = defaultdict(int)
        self.seconds: dict[str, float] = defaultdict(float)
        self.stmts: dict[str, int] = defaultdict(int)
        self._originals: list[tuple[Any, str, Any]] = []

    def wrap(self, module: Any, name: str, label: str | None = None) -> None:
        original = getattr(module, name, None)
        if original is None:
            return
        self._originals.append((module, name, original))
        etiqueta = label or name

        async def _wrapped(*a: Any, **kw: Any) -> Any:
            t0 = time.perf_counter()
            s0 = self.profile.total
            try:
                return await original(*a, **kw)
            finally:
                self.calls[etiqueta] += 1
                self.seconds[etiqueta] += time.perf_counter() - t0
                self.stmts[etiqueta] += self.profile.total - s0

        setattr(module, name, _wrapped)

    def restore(self) -> None:
        """Deshace los monkeypatch. Un script que termina no lo necesita; un test que
        corre en el mismo proceso que los siguientes, sí."""
        for module, name, original in reversed(self._originals):
            setattr(module, name, original)
        self._originals.clear()

    def report(self) -> None:
        p("TIEMPO POR FASE (funciones envueltas)")
        print(f"  {'llamadas':>9}  {'seg':>8}  {'stmts':>8}  {'stmt/llamada':>12}  función")
        print(f"  {'-' * 9}  {'-' * 8}  {'-' * 8}  {'-' * 12}  {'-' * 34}")
        for name in sorted(self.seconds, key=lambda x: -self.seconds[x]):
            n = self.calls[name]
            por = self.stmts[name] / n if n else 0
            print(
                f"  {n:>9}  {self.seconds[name]:>8.2f}  {self.stmts[name]:>8}  "
                f"{por:>12.1f}  {name}"
            )
