"""Read-only: ventas IMPORTADAS cuyo detalle dice que no son ventas (D0, 2026-10-06).

Hasta el arreglo D0, ``parse_libro_diario`` mandaba a ventas TODA fila con
"Dinero / Ingreso": un aporte de socio, un préstamo o una devolución se
importaban como facturación. Este script mide si eso ya pasó en la base: busca
ventas vivas que vinieron de un archivo y cuyo detalle (``notes``) el vocabulario
de tipos (``app/domain/tipo_de_movimiento``, el MISMO que usa el arreglo)
reconoce como un movimiento que no es venta.

SOLO SELECT. No repara nada: si aparecen filas, la reparación es un plan aparte.
Nunca imprime la connection URL. Corre desde ``backend/`` (importa ``app.domain``).

Usage:
    DATABASE_URL=... .venv/bin/python scripts/diag_ventas_que_no_son_ventas.py
    DATABASE_URL=... .venv/bin/python scripts/diag_ventas_que_no_son_ventas.py dueño@mail.com
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
from _db import normalize_dsn  # noqa: E402

from app.domain.tipo_de_movimiento import clasificar, es_reconocido  # noqa: E402

_EJEMPLOS = 5


def _no_es_venta(detalle: str | None) -> str | None:
    """El motivo si el detalle nombra un movimiento que no es venta, si no ``None``.
    Mismo criterio que ``parse_libro_diario`` después de D0."""
    if not detalle or not detalle.strip():
        return None
    tipo = clasificar(detalle)
    if tipo.destino == "otros" and es_reconocido(detalle):
        return tipo.motivo
    return None


async def main(email: str | None) -> None:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        filtro, args = "", []
        if email:
            u = await conn.fetchrow(
                "SELECT tenant_id FROM users WHERE lower(email)=lower($1) LIMIT 1", email
            )
            if not u:
                print("  ⚠ usuario inexistente.")
                return
            filtro, args = " AND s.tenant_id = $1", [u["tenant_id"]]
        rows = await conn.fetch(
            "SELECT s.tenant_id, s.id, date(s.transaction_date) AS d, s.amount, s.notes, "
            "u.original_filename "
            "FROM sales_entries s JOIN uploaded_files u ON u.id = s.source_upload_id "
            "WHERE s.voided_at IS NULL AND s.source_upload_id IS NOT NULL" + filtro,
            *args,
        )
        print(f"Ventas importadas vivas revisadas: {len(rows)}")

        por_tenant: dict[str, list[tuple[str, asyncpg.Record]]] = defaultdict(list)
        for r in rows:
            motivo = _no_es_venta(r["notes"])
            if motivo:
                por_tenant[str(r["tenant_id"])].append((motivo, r))

        if not por_tenant:
            print("✅ Ninguna venta importada tiene un detalle que diga que no es venta.")
            return

        for tid, hallazgos in sorted(por_tenant.items(), key=lambda x: -len(x[1])):
            owner = await conn.fetchval(
                "SELECT email FROM users WHERE tenant_id=$1 ORDER BY created_at LIMIT 1",
                hallazgos[0][1]["tenant_id"],
            )
            total = sum((Decimal(r["amount"]) for _, r in hallazgos), Decimal(0))
            print(f"\n== tenant {tid} ({owner}): {len(hallazgos)} venta(s), ${total:,.2f}")
            por_motivo: dict[str, list[asyncpg.Record]] = defaultdict(list)
            for motivo, r in hallazgos:
                por_motivo[motivo.split(":")[0]].append(r)
            for motivo, rs in sorted(por_motivo.items(), key=lambda x: -len(x[1])):
                suma = sum((Decimal(r["amount"]) for r in rs), Decimal(0))
                print(f"   {motivo}: {len(rs)} venta(s), ${suma:,.2f}")
                for r in rs[:_EJEMPLOS]:
                    print(
                        f"      {r['d']}  ${Decimal(r['amount']):>14,.2f}  "
                        f"«{(r['notes'] or '')[:50]}»  [{r['original_filename']}]"
                    )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
