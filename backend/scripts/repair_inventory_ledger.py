"""Repara el ledger de inventory_movements inflado por relecturas no idempotentes.

Sistémico, conservador, reversible, dry-run por defecto. Corre EN ORDEN los tres pasos
de una misma reparación (misma corrida, misma transacción):

  B1 — DEDUP de duplicados de relectura (AMBOS lados: compras Y ventas).
       Cluster = (tenant_id, product_id, date(COALESCE(occurred_at, created_at)), qty,
       unit_cost, movement_type) con COUNT(*) > 1 sobre movimientos VIVOS (voided_at IS
       NULL). La clave de cluster es la fecha de NEGOCIO del movimiento, no la fecha de
       carga del archivo (``created_at``) — dos compras reales de meses distintos
       cargadas el mismo día NO deben agruparse en el mismo cluster (incidente 2026-07,
       "don pedro": el dedup por ``date(created_at)`` voideó compras válidas). Solo se
       actúa sobre clusters HIGH_CONFIDENCE; MEDIUM/LOW se reportan y NO se tocan. Los
       extras se marcan con voided_at = now() (soft-delete), conservando el más antiguo.

  B2 — BACKFILL del COGS faltante (regla contable: toda compra de mercadería es un gasto
       COGS que entra al stock). Por cada movimiento de COMPRA vivo sin un ExpenseEntry
       COGS que lo respalde, crea el gasto: amount = unit_cost × qty, fecha =
       products.acquired_at (o created_at del movimiento), product_id ligado,
       expense_type='COGS', category='INVENTORY', supplier_id del movimiento. Lo dudoso
       (unit_cost NULL, o hay un COGS del producto con monto que no matchea) se reporta y
       NO se crea a ciegas.

  B3 — AJUSTE INCREMENTAL del stock. Descuenta de products.stock_units +
       inventory_balances.current_qty la qty EXACTA de los movimientos que B1 voideó,
       agrupada por producto, con clamp a >= 0. NO recomputa desde el ledger (Σ qty vivos)
       porque stock_units tiene base no-ledger (alta manual, chat, seed, catálogo con
       stock absoluto) que un recompute destruiría; solo se resta lo que los duplicados
       aportaron de más.

CÓMO SE DEFINE HIGH_CONFIDENCE (B1) — se exige firma de relectura, no solo el cluster:
  1. SHARED_ROW_HASH: dentro del cluster, un mismo source_row_hash NO nulo aparece >1 vez
     entre los movimientos vivos. source_row_hash es la identidad lógica estable de una
     fila importada (idempotencia): que exista más de una viva es prueba directa de que la
     MISMA fila se importó varias veces. Se conserva la más antigua por hash; los extras
     de ese hash se voidan. (Los movimientos con hash NULL o hash único NO se tocan por
     esta vía — no hay prueba de relectura.)
  2. TRIPLICATE_SAME_DAY: el cluster tiene n >= 3 movimientos idénticos el mismo día para
     el mismo producto/qty/costo. Nadie registra a mano la misma compra 3+ veces el mismo
     día; es firma de reread. Se conserva el más antiguo, se voidan los n-1 restantes.
  3. TIGHT_TIMING: cluster n==2 sin hash compartido, AMBOS movimientos sin
     source_upload_id (manual/legacy), creados a menos de _TIGHT_TIMING_SECONDS (5s)
     uno del otro. Ningún humano genera dos filas idénticas (mismo producto/tipo/
     día/qty/costo) separadas por milisegundos — es doble insert programático (retry,
     bug de loop, request duplicado). El origen decide qué firma de timing es válida
     (ver nota IMPORTANTE más abajo): esta regla NUNCA se aplica si alguno de los dos
     tiene source_upload_id.
  4. BATCH_TIMING: cluster n==2 sin hash compartido, AMBOS sin source_upload_id, con
     timing NO ajustado, pero cuyo delta (redondeado a ms) se repite en
     >= _BATCH_DELTA_MIN_OCCURRENCES clusters del MISMO tenant. Un delta idéntico entre
     pares de movimientos DISTINTOS solo ocurre si un job/script corrió dos veces sobre
     el mismo batch — es matemáticamente imposible como coincidencia real. Requiere ver
     TODOS los clusters del tenant antes de decidir (por eso B1 corre en dos pases: el
     segundo agrupa los "PENDING" por delta).
     Caso real que motivó esto (2026-07): un tenant tenía 150 clusters LOW de tipo
     'adjustment' (sin source_upload_id, así que invisibles al heurístico original) — 104
     compartían el delta 1621.438s exacto y 46 tenían delta ~0s. Recién se pescaron
     comparando manualmente el delta entre TODOS los clusters, algo que hoy hace
     automáticamente esta sección y queda auditado en decision_data.by_reason.

  IMPORTANTE — "timing ajustado = duplicado" solo vale para movimientos SIN
  source_upload_id. Si ambos miembros de un par n==2 vienen de archivo (source_upload_id
  no nulo en los dos), timing ajustado entre filas es NORMAL: un insert masivo carga N
  filas en milisegundos, así que NO es evidencia de duplicado — ni TIGHT_TIMING ni
  BATCH_TIMING se le aplican, sin importar qué tan chico sea el delta. Sin
  source_row_hash compartido (paso 1) no hay prueba de que sea la MISMA fila reimportada.

  MEDIUM/LOW (solo reporte, NO se toca) — lo que queda después de descartar 1-4:
    - LOW (reason=IMPORT_BATCH_TIMING_INCONCLUSIVE): n == 2 sin hash, AMBOS con
      source_upload_id (mismo tipo de origen archivo) — timing ajustado o no, sin hash
      compartido no hay prueba de duplicado; es el patrón normal de un insert masivo.
    - MEDIUM (reason=MIXED_ORIGIN_REVIEW): n == 2 sin hash, origen MIXTO (uno con
      source_upload_id, el otro sin) — no hay base para asumir un único origen.
    - LOW (reason=MANUAL_PAIR_LOW): n == 2 sin hash, AMBOS sin source_upload_id, delta
      grande y no repetido (no pasó el pase 2) → probablemente dos eventos reales.

DETECCIÓN RÁPIDA A FUTURO: cada cluster reportado trae `reason` (y `delta_seconds` si
aplica). El audit log de una corrida con --apply guarda `by_reason` (conteo por razón) en
decision_data — si vuelve a aparecer un BATCH_TIMING con conteo alto, es la señal
inmediata de "un job corrió dos veces": revisar decision_audit_log/pipeline_events/logs
de Railway alrededor de los timestamps de esos movimientos (no hace falta re-derivar el
delta a mano como la primera vez).

CHECK DE "SIN GASTO COGS" (B2) — ROBUSTO, no solo mismo día. El movimiento se crea en
fecha de import y el gasto en fecha de compra, así que se matchea por producto + monto
dentro de una VENTANA de fechas (--cogs-window-days, ancla = acquired_at o created_at):
    NOT EXISTS un expense COGS vivo del mismo product_id con
    ABS(amount - unit_cost×qty) <= tolerancia (--amount-tol-pct, mínimo $1) dentro de la
    ventana. Si hay un COGS del producto en la ventana pero con monto que NO matchea →
    AMOUNT_MISMATCH (se reporta, NO se crea). unit_cost NULL → NO_COST (se reporta).

AUDITORÍA: cada paso inserta en decision_audit_log con decision_type='INVENTORY_REPAIR'
y decision_data {step: B1|B2|B3, ...ids, confidence}. Los gastos creados por el backfill
llevan custom_fields["_repair_backfill"]=true (para encontrarlos en la reversa).

REVERSA DE ESTE SCRIPT:
  B1: limpiar voided_at de los movimientos voidados (decision_data.voided_movement_ids).
  B2: borrar los ExpenseEntry con custom_fields->>'_repair_backfill' = 'true'
      (decision_data.created_expense_ids).
  B3: se re-deriva del ledger (volver a correr el recompute tras revertir B1).

COMPUERTA DE EJECUCIÓN (apply por tandas — datos de PRODUCCIÓN):
  1. Dry-run global:   scripts/repair_inventory_ledger.py --all-active --out r.csv
  2. Revisar el reporte (sobre todo MEDIUM/LOW de B1 y los dudosos de B2).
  3. Apply a UN tenant conocido:
       scripts/repair_inventory_ledger.py --tenant ee2625dc-96b7-464c-bda3-7f7018cc2a5b --apply
  4. Verificar con: scripts/diag_inventory_inflation.py --tenant <uuid>
  5. Recién después, por batches / global: ... --all-active --apply

SOLO SELECT en dry-run; escritura únicamente bajo --apply. NUNCA imprime la connection
URL (la DATABASE_URL la provee el usuario desde su shell). Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_DECISION_TYPE = "INVENTORY_REPAIR"
_TRIGGERED_BY = "script:repair_inventory_ledger"
_COGS_CATEGORY = "INVENTORY"
_BACKFILL_FLAG = "_repair_backfill"

# Confianza de un cluster de duplicados (B1).
_HIGH = "HIGH_CONFIDENCE"
_MEDIUM = "MEDIUM"
_LOW = "LOW"
_PENDING = "_PENDING_TIMING"  # interno: n==2 sin hash, a la espera del pase 2 (batch delta)

# Razón puntual de un HIGH/MEDIUM/LOW — para auditoría y detección rápida a futuro.
_REASON_SHARED_HASH = "SHARED_ROW_HASH"
_REASON_TRIPLICATE = "TRIPLICATE_SAME_DAY"
_REASON_TIGHT_TIMING = "TIGHT_TIMING"  # par duplicado creado casi en el mismo instante
_REASON_BATCH_TIMING = "BATCH_TIMING"  # delta que se repite en muchos clusters del tenant
_REASON_MANUAL_PAIR_LOW = "MANUAL_PAIR_LOW"
# n==2 con AMBOS movimientos taggeados al mismo tipo de origen archivo
# (source_upload_id no nulo en los dos): timing ajustado entre filas es NORMAL en un
# insert masivo (un import carga N filas en milisegundos) — no es evidencia de
# duplicado por sí sola. Sin source_row_hash compartido no hay prueba de que sea la
# MISMA fila reimportada, así que queda LOW y no se toca.
_REASON_IMPORT_BATCH_TIMING_INCONCLUSIVE = "IMPORT_BATCH_TIMING_INCONCLUSIVE"
# n==2 con origen MIXTO (uno con source_upload_id, el otro sin) — no hay base para
# asumir un único origen ni aplicar con confianza la regla de timing de ninguno de
# los dos casos. Revisión humana.
_REASON_MIXED_ORIGIN_REVIEW = "MIXED_ORIGIN_REVIEW"

# Umbrales de detección de duplicados por TIMING (sin source_row_hash disponible, ej.
# movement_type='adjustment' generado por jobs/scripts que no taggean origen). Cubre el
# caso real detectado 2026-07: 150 clusters "LOW" en un tenant resultaron ser duplicados
# de un script/job corrido dos veces — 104 compartían el MISMO delta de 1621.438s entre
# sus 2 movimientos, y 46 tenían delta ~0s (mismo instante, doble insert). Ninguno tenía
# source_upload_id, así que el heurístico original (que solo mira hash/upload_id) los
# clasificaba LOW y quedaban invisibles sin horas de diagnóstico manual.
_TIGHT_TIMING_SECONDS = 5.0
_BATCH_DELTA_ROUND = 3  # precisión en ms al agrupar deltas entre clusters
_BATCH_DELTA_MIN_OCCURRENCES = 3  # delta repetido ≥3 veces → job/script duplicado, no azar

# Disposición de un movimiento de compra frente al backfill de COGS (B2).
_B2_CREATE = "CREATE"
_B2_HAS_COGS = "HAS_COGS"  # ya existe un gasto que lo respalda
_B2_NO_COST = "NO_COST"  # unit_cost NULL → no se puede calcular, se reporta
_B2_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"  # hay COGS del producto pero el monto no matchea


# --------------------------------------------------------------------------- B1


async def _plan_b1_dedup(session: AsyncSession, tid: uuid.UUID) -> dict[str, Any]:
    """Plan de dedup del tenant (read-only). Clasifica clusters y elige los void_ids.

    Dos pases: 1) hash compartido / triplicado / timing ajustado (por cluster, sin
    contexto externo); 2) delta de timing compartido por ≥3 clusters del tenant (batch
    duplicado — requiere ver TODOS los clusters primero). Devuelve {clusters: [...],
    void_ids: [...], affected_products: [...], by_conf: Counter, by_reason: Counter}.
    """
    clusters = (
        await session.execute(
            text(
                "SELECT product_id, movement_type, "
                "       date(COALESCE(occurred_at, created_at)) AS d, qty, unit_cost, "
                "       COUNT(*) AS n "
                "FROM inventory_movements "
                "WHERE tenant_id = :tid AND voided_at IS NULL "
                "GROUP BY product_id, movement_type, "
                "         date(COALESCE(occurred_at, created_at)), qty, unit_cost "
                "HAVING COUNT(*) > 1"
            ),
            {"tid": tid},
        )
    ).mappings().all()

    resolved: list[dict[str, Any]] = []  # clusters ya clasificados (pase 1)
    pending: list[dict[str, Any]] = []  # n==2 sin hash, esperan el pase 2

    for c in clusters:
        members = (
            await session.execute(
                text(
                    "SELECT id, source_row_hash, source_upload_id, created_at "
                    "FROM inventory_movements "
                    "WHERE tenant_id = :tid AND voided_at IS NULL "
                    "AND product_id = :pid AND movement_type = :mt "
                    "AND date(COALESCE(occurred_at, created_at)) = :d AND qty = :qty "
                    "AND unit_cost IS NOT DISTINCT FROM :uc "
                    "ORDER BY created_at ASC"
                ),
                {
                    "tid": tid,
                    "pid": c["product_id"],
                    "mt": c["movement_type"],
                    "d": c["d"],
                    "qty": c["qty"],
                    "uc": c["unit_cost"],
                },
            )
        ).mappings().all()

        base = {
            "product_id": str(c["product_id"]),
            "movement_type": str(c["movement_type"]),
            "day": str(c["d"]),
            "qty": int(c["qty"]),
            "n": int(c["n"]),
        }
        confidence, reason, cluster_voids, delta = _classify_cluster(members)
        if confidence == _PENDING:
            pending.append({**base, "members": members, "delta": delta})
        else:
            resolved.append(
                {
                    **base,
                    "confidence": confidence,
                    "reason": reason,
                    "_void_ids": cluster_voids,
                }
            )

    # Pase 2: entre los PENDING, un delta (redondeado a ms) que se repite ≥N veces es
    # evidencia de un job/script corrido más de una vez — no de coincidencia manual.
    delta_counts: Counter[float] = Counter(
        round(p["delta"], _BATCH_DELTA_ROUND) for p in pending
    )
    for p in pending:
        rounded = round(p["delta"], _BATCH_DELTA_ROUND)
        members = p["members"]
        if delta_counts[rounded] >= _BATCH_DELTA_MIN_OCCURRENCES:
            confidence, reason = _HIGH, _REASON_BATCH_TIMING
            cluster_voids = [str(members[1]["id"])]
        else:
            # Solo llegan acá clusters both_untagged (both_import se resuelve antes,
            # en _classify_cluster) — delta grande y aislado, sin tag de archivo:
            # probablemente dos eventos manuales reales.
            confidence, reason = _LOW, _REASON_MANUAL_PAIR_LOW
            cluster_voids = []
        resolved.append(
            {
                "product_id": p["product_id"],
                "movement_type": p["movement_type"],
                "day": p["day"],
                "qty": p["qty"],
                "n": p["n"],
                "confidence": confidence,
                "reason": reason,
                "delta_seconds": rounded,
                "_void_ids": cluster_voids,
            }
        )

    void_ids: list[str] = []
    affected: set[str] = set()
    by_conf: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    reported: list[dict[str, Any]] = []
    for row in resolved:
        by_conf[row["confidence"]] += 1
        by_reason[row["reason"]] += 1
        cluster_voids = row.pop("_void_ids")
        row["void_count"] = len(cluster_voids)
        reported.append(row)
        if row["confidence"] == _HIGH and cluster_voids:
            void_ids.extend(cluster_voids)
            affected.add(row["product_id"])

    return {
        "clusters": reported,
        "void_ids": void_ids,
        "affected_products": sorted(affected),
        "by_conf": by_conf,
        "by_reason": by_reason,
    }


def _classify_cluster(
    members: Sequence[Any],
) -> tuple[str, str | None, list[str], float | None]:
    """Clasifica un cluster de movimientos idénticos (pase 1, sin contexto externo).

    Devuelve (confidence, reason, void_ids, delta_seconds). ``confidence == _PENDING``
    significa "n==2 sin hash compartido, no se puede decidir sin ver los demás
    clusters del tenant" — el pase 2 (en ``_plan_b1_dedup``) lo resuelve.
    """
    # 1. SHARED_ROW_HASH: la misma fila lógica importada más de una vez.
    by_hash: dict[str, list[Any]] = {}
    for m in members:
        h = m["source_row_hash"]
        if h:
            by_hash.setdefault(str(h), []).append(m)
    hash_voids: list[str] = []
    for group in by_hash.values():
        if len(group) > 1:
            # conservar el más antiguo (ya vienen ORDER BY created_at ASC), voidar el resto
            hash_voids.extend(str(m["id"]) for m in group[1:])
    if hash_voids:
        return _HIGH, _REASON_SHARED_HASH, hash_voids, None

    # 2. TRIPLICATE_SAME_DAY: 3+ idénticos el mismo día → firma de reread.
    if len(members) >= 3:
        return _HIGH, _REASON_TRIPLICATE, [str(m["id"]) for m in members[1:]], None

    # 3. n == 2 sin hash compartido: el origen decide qué firma de timing es válida.
    # "Timing ajustado = duplicado" solo vale para movimientos SIN source_upload_id
    # (manual/legacy) — ahí ningún humano genera dos filas idénticas (mismo
    # producto/tipo/día/qty/costo) separadas por milisegundos. Si ambos vienen del
    # MISMO tipo de origen archivo (source_upload_id no nulo en los dos), timing
    # ajustado es NORMAL: un insert masivo carga N filas en milisegundos, y sin
    # source_row_hash compartido (ya descartado en el paso 1) no hay prueba de que
    # sea la MISMA fila reimportada.
    both_import = all(m["source_upload_id"] is not None for m in members)
    both_untagged = all(m["source_upload_id"] is None for m in members)
    delta = abs((members[1]["created_at"] - members[0]["created_at"]).total_seconds())

    if both_import:
        # Timing chico o no, sin hash compartido no hay evidencia de duplicado real
        # para pares de archivo — no fluye al pase 2 (ahí "delta repetido" sería
        # justamente el patrón normal de un insert masivo, no un job corrido 2 veces).
        return _LOW, _REASON_IMPORT_BATCH_TIMING_INCONCLUSIVE, [], delta

    if not both_untagged:
        # Origen mixto: un lado tagueado y el otro no. No hay base para asumir un
        # único origen ni aplicar con confianza ninguna de las dos reglas de timing.
        return _MEDIUM, _REASON_MIXED_ORIGIN_REVIEW, [], delta

    # both_untagged: manual/legacy sin source_upload_id — comportamiento original,
    # sin cambios.
    if delta < _TIGHT_TIMING_SECONDS:
        return _HIGH, _REASON_TIGHT_TIMING, [str(members[1]["id"])], None

    # Delta más grande y aislado, ambos sin tag: puede ser timing de un job repetido
    # (ver pase 2) o dos eventos reales — queda pendiente hasta comparar con el resto
    # del tenant.
    return _PENDING, None, [], delta


async def _apply_b1_dedup(
    session: AsyncSession, tid: uuid.UUID, plan: dict[str, Any]
) -> int:
    """Voida (soft-delete) los movimientos duplicados HIGH_CONFIDENCE. Auditado."""
    void_ids: list[str] = plan["void_ids"]
    if not void_ids:
        return 0
    for vid in void_ids:
        await session.execute(
            text(
                "UPDATE inventory_movements SET voided_at = now() "
                "WHERE tenant_id = :tid AND id = CAST(:vid AS uuid) AND voided_at IS NULL"
            ),
            {"tid": tid, "vid": vid},
        )
    await _audit(
        session,
        tid,
        {
            "step": "B1",
            "confidence": _HIGH,
            "voided_movement_ids": void_ids,
            "voided_count": len(void_ids),
            "by_confidence": dict(plan["by_conf"]),
            # by_reason: para detectar rápido si esto vuelve a pasar. Un BATCH_TIMING
            # con conteo alto en una corrida futura es la señal de "job corrido 2 veces"
            # sin necesitar horas de diagnóstico manual como la vez que esto se descubrió.
            "by_reason": dict(plan["by_reason"]),
        },
    )
    return len(void_ids)


# --------------------------------------------------------------------------- B2


async def _plan_b2_backfill(
    session: AsyncSession,
    tid: uuid.UUID,
    exclude_movement_ids: set[str],
    window_days: int,
    tol_pct: float,
) -> dict[str, Any]:
    """Plan de backfill de COGS del tenant (read-only). Excluye los movs que B1 voidaría.

    Devuelve {to_create: [...], by_disposition: Counter}. Cada to_create trae los datos
    necesarios para insertar el ExpenseEntry.
    """
    movements = (
        await session.execute(
            text(
                "SELECT im.id, im.product_id, im.qty, im.unit_cost, im.supplier_id, "
                "       COALESCE(p.acquired_at, im.created_at) AS anchor "
                "FROM inventory_movements im "
                "LEFT JOIN products p ON p.id = im.product_id "
                "WHERE im.tenant_id = :tid AND im.movement_type = 'purchase' "
                "AND im.voided_at IS NULL"
            ),
            {"tid": tid},
        )
    ).mappings().all()

    to_create: list[dict[str, Any]] = []
    by_disp: Counter[str] = Counter()

    for m in movements:
        mid = str(m["id"])
        if mid in exclude_movement_ids:
            continue  # B1 lo voidaría → no debe recibir COGS
        unit_cost = m["unit_cost"]
        if unit_cost is None:
            by_disp[_B2_NO_COST] += 1
            continue
        expected = Decimal(str(unit_cost)) * Decimal(int(m["qty"]))
        tol = max(Decimal("1"), (expected.copy_abs() * Decimal(str(tol_pct))))

        # anchor puede venir de inventory_movements.created_at (timestamptz) o de
        # products.acquired_at (naive) — expense_entries.transaction_date es naive
        # (SIN timezone), así que asyncpg no puede bindear un datetime aware contra
        # esa columna ("can't subtract offset-naive and offset-aware datetimes").
        anchor: datetime = m["anchor"]
        if anchor.tzinfo is not None:
            anchor = anchor.astimezone(UTC).replace(tzinfo=None)

        disposition = await _cogs_disposition(
            session, tid, str(m["product_id"]), expected, tol, anchor, window_days
        )
        by_disp[disposition] += 1
        if disposition == _B2_CREATE:
            to_create.append(
                {
                    "movement_id": mid,
                    "product_id": str(m["product_id"]),
                    "supplier_id": str(m["supplier_id"]) if m["supplier_id"] else None,
                    "amount": str(expected),
                    "transaction_date": anchor,
                }
            )

    return {"to_create": to_create, "by_disposition": by_disp}


async def _cogs_disposition(
    session: AsyncSession,
    tid: uuid.UUID,
    product_id: str,
    expected: Decimal,
    tol: Decimal,
    anchor: Any,
    window_days: int,
) -> str:
    """Decide si un movimiento de compra necesita COGS. Ventana de fechas + tolerancia."""
    # asyncpg bindea CAST(:window AS interval) esperando un timedelta de Python, no un
    # string ("60 days") — el codec de interval llama .days sobre el parámetro.
    window = timedelta(days=window_days)
    match = (
        await session.execute(
            text(
                "SELECT 1 FROM expense_entries "
                "WHERE tenant_id = :tid AND product_id = CAST(:pid AS uuid) "
                "AND expense_type = 'COGS' AND voided_at IS NULL "
                "AND ABS(amount - :expected) <= :tol "
                "AND transaction_date BETWEEN "
                "    (CAST(:anchor AS timestamp) - CAST(:window AS interval)) "
                "AND (CAST(:anchor AS timestamp) + CAST(:window AS interval)) "
                "LIMIT 1"
            ),
            {
                "tid": tid,
                "pid": product_id,
                "expected": expected,
                "tol": tol,
                "anchor": anchor,
                "window": window,
            },
        )
    ).first()
    if match is not None:
        return _B2_HAS_COGS

    other = (
        await session.execute(
            text(
                "SELECT 1 FROM expense_entries "
                "WHERE tenant_id = :tid AND product_id = CAST(:pid AS uuid) "
                "AND expense_type = 'COGS' AND voided_at IS NULL "
                "AND transaction_date BETWEEN "
                "    (CAST(:anchor AS timestamp) - CAST(:window AS interval)) "
                "AND (CAST(:anchor AS timestamp) + CAST(:window AS interval)) "
                "LIMIT 1"
            ),
            {
                "tid": tid,
                "pid": product_id,
                "anchor": anchor,
                "window": window,
            },
        )
    ).first()
    # Hay un COGS del producto en la ventana pero el monto no matchea → dudoso, no se crea.
    return _B2_AMOUNT_MISMATCH if other is not None else _B2_CREATE


async def _apply_b2_backfill(
    session: AsyncSession, tid: uuid.UUID, plan: dict[str, Any]
) -> int:
    """Crea los ExpenseEntry COGS faltantes. Marcados con _repair_backfill. Auditado."""
    to_create: list[dict[str, Any]] = plan["to_create"]
    if not to_create:
        return 0
    created_ids: list[str] = []
    for item in to_create:
        new_id = str(uuid.uuid4())
        custom_fields = {
            _BACKFILL_FLAG: True,
            "_source_movement_id": item["movement_id"],
        }
        await session.execute(
            text(
                "INSERT INTO expense_entries "
                "(id, tenant_id, product_id, supplier_id, amount, category, expense_type, "
                " transaction_date, description, is_recurring, payment_method, provenance, "
                " custom_fields, created_at, updated_at) "
                "VALUES (CAST(:id AS uuid), :tid, CAST(:pid AS uuid), "
                " CASE WHEN :sid IS NULL THEN NULL ELSE CAST(:sid AS uuid) END, "
                " :amount, :cat, 'COGS', CAST(:tdate AS timestamp), :descr, false, "
                " 'transfer', 'REAL', CAST(:cf AS jsonb), now(), now())"
            ),
            {
                "id": new_id,
                "tid": tid,
                "pid": item["product_id"],
                "sid": item["supplier_id"],
                "amount": item["amount"],
                "cat": _COGS_CATEGORY,
                "tdate": item["transaction_date"],
                "descr": "Backfill COGS (reparación de ledger) — compra de mercadería",
                "cf": json.dumps(custom_fields),
            },
        )
        created_ids.append(new_id)
    await _audit(
        session,
        tid,
        {
            "step": "B2",
            "created_expense_ids": created_ids,
            "created_count": len(created_ids),
            "source_movement_ids": [i["movement_id"] for i in to_create],
        },
    )
    return len(created_ids)


# --------------------------------------------------------------------------- B3


async def _apply_b3_adjust(session: AsyncSession, tid: uuid.UUID, void_ids: list[str]) -> int:
    """Descuenta del stock la qty EXACTA de los movimientos que B1 voideó (incremental).

    NO recomputa ``stock_units`` desde el ledger: ``stock_units`` tiene base NO-ledger
    (alta manual vía POST /products, chat, seed, y el catálogo que setea stock absoluto
    pero registra solo el delta como movimiento), que un ``Σ(qty)`` del ledger destruiría
    (bajaría un producto con 50 reales a los pocos movimientos vivos). Se resta SOLO lo
    que los duplicados aportaron de más. Clamp a 0 como piso final. Auditado.
    """
    if not void_ids:
        return 0
    # Σ qty de los movimientos voidados por ESTA corrida, agrupado por producto.
    per_product: dict[str, int] = {}
    for vid in void_ids:
        row = (
            await session.execute(
                text(
                    "SELECT product_id, qty FROM inventory_movements "
                    "WHERE tenant_id = :tid AND id = CAST(:vid AS uuid)"
                ),
                {"tid": tid, "vid": vid},
            )
        ).mappings().first()
        if row is not None:
            pid = str(row["product_id"])
            per_product[pid] = per_product.get(pid, 0) + int(row["qty"])
    for pid, dq in per_product.items():
        await session.execute(
            text(
                "UPDATE products SET stock_units = GREATEST(0, stock_units - :dq) "
                "WHERE tenant_id = :tid AND id = CAST(:pid AS uuid)"
            ),
            {"tid": tid, "pid": pid, "dq": dq},
        )
        await session.execute(
            text(
                "UPDATE inventory_balances SET current_qty = GREATEST(0, current_qty - :dq) "
                "WHERE tenant_id = :tid AND product_id = CAST(:pid AS uuid)"
            ),
            {"tid": tid, "pid": pid, "dq": dq},
        )
    await _audit(
        session,
        tid,
        {
            "step": "B3",
            "adjusted_products": len(per_product),
            "removed_qty_total": sum(per_product.values()),
        },
    )
    return len(per_product)


# --------------------------------------------------------------------------- infra


async def _audit(session: AsyncSession, tid: uuid.UUID, decision_data: dict[str, Any]) -> None:
    """Inserta una fila en decision_audit_log (insert-only)."""
    await session.execute(
        text(
            "INSERT INTO decision_audit_log "
            "(id, tenant_id, decision_type, decision_data, triggered_by, created_at) "
            "VALUES (gen_random_uuid(), :tid, :dt, CAST(:dd AS jsonb), :tb, now())"
        ),
        {
            "tid": tid,
            "dt": _DECISION_TYPE,
            "dd": json.dumps(decision_data),
            "tb": _TRIGGERED_BY,
        },
    )


def _print_tenant(
    tid: uuid.UUID, b1: dict[str, Any], b2: dict[str, Any]
) -> None:
    by_conf = b1["by_conf"]
    by_reason = b1["by_reason"]
    by_disp = b2["by_disposition"]
    print(f"tenant {tid}:")
    print(
        f"  B1 dedup: {len(b1['clusters'])} cluster(s) — {dict(by_conf)}  "
        f"→ {len(b1['void_ids'])} movimiento(s) a voidar (HIGH), "
        f"{len(b1['affected_products'])} producto(s) afectado(s)"
    )
    print(f"  B1 por razón: {dict(by_reason)}")
    batch_count = by_reason.get(_REASON_BATCH_TIMING, 0)
    if batch_count:
        deltas = sorted(
            {
                c["delta_seconds"]
                for c in b1["clusters"]
                if c["reason"] == _REASON_BATCH_TIMING
            }
        )
        print(
            f"  ⚠ ALERTA: {batch_count} cluster(s) con timing BATCH_TIMING "
            f"(delta compartido: {deltas}s) — evidencia de un job/script corrido más "
            "de una vez. Buscar en logs/decision_audit_log alrededor de esos horarios."
        )
    print(
        f"  B2 backfill COGS: {len(b2['to_create'])} gasto(s) a crear — disposiciones "
        f"{dict(by_disp)}"
    )


def _write_report(path: str, rows: list[dict[str, Any]]) -> None:
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
    print(f"\nReporte escrito en {path} ({len(rows)} fila(s)).")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repara el ledger de inventario (dedup + backfill COGS + recompute)."
    )
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument("--out", help="Path del reporte (CSV si .csv, si no JSON)")
    parser.add_argument(
        "--cogs-window-days",
        type=int,
        default=60,
        help="Ventana ± (días) para matchear un COGS existente en B2 (default: 60)",
    )
    parser.add_argument(
        "--amount-tol-pct",
        type=float,
        default=0.02,
        help="Tolerancia relativa de monto para el match de COGS (default: 0.02 = 2%%)",
    )
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: indicá --tenant <uuid> o --all-active.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        if args.tenant:
            tids = [uuid.UUID(args.tenant)]
        else:
            rows = await session.execute(
                text("SELECT tenant_id FROM tenants WHERE status = 'ACTIVE'")
            )
            tids = [r[0] for r in rows.all()]

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] reparación de ledger de inventario en {len(tids)} tenant(s)\n")

        report_rows: list[dict[str, Any]] = []
        tot_voided = tot_created = tot_recomputed = 0

        for tid in tids:
            b1 = await _plan_b1_dedup(session, tid)
            b2 = await _plan_b2_backfill(
                session,
                tid,
                exclude_movement_ids=set(b1["void_ids"]),
                window_days=args.cogs_window_days,
                tol_pct=args.amount_tol_pct,
            )
            if not b1["clusters"] and not b2["to_create"] and not b2["by_disposition"]:
                continue

            _print_tenant(tid, b1, b2)
            report_rows.append(
                {
                    "tenant_id": str(tid),
                    "b1_clusters": len(b1["clusters"]),
                    "b1_high": b1["by_conf"].get(_HIGH, 0),
                    "b1_medium": b1["by_conf"].get(_MEDIUM, 0),
                    "b1_low": b1["by_conf"].get(_LOW, 0),
                    "b1_movements_to_void": len(b1["void_ids"]),
                    "b1_affected_products": len(b1["affected_products"]),
                    "b1_reason_shared_hash": b1["by_reason"].get(_REASON_SHARED_HASH, 0),
                    "b1_reason_triplicate": b1["by_reason"].get(_REASON_TRIPLICATE, 0),
                    "b1_reason_tight_timing": b1["by_reason"].get(_REASON_TIGHT_TIMING, 0),
                    "b1_reason_batch_timing": b1["by_reason"].get(_REASON_BATCH_TIMING, 0),
                    "b1_reason_import_batch_inconclusive": b1["by_reason"].get(
                        _REASON_IMPORT_BATCH_TIMING_INCONCLUSIVE, 0
                    ),
                    "b1_reason_mixed_origin_review": b1["by_reason"].get(
                        _REASON_MIXED_ORIGIN_REVIEW, 0
                    ),
                    "b2_to_create": len(b2["to_create"]),
                    "b2_has_cogs": b2["by_disposition"].get(_B2_HAS_COGS, 0),
                    "b2_no_cost": b2["by_disposition"].get(_B2_NO_COST, 0),
                    "b2_amount_mismatch": b2["by_disposition"].get(_B2_AMOUNT_MISMATCH, 0),
                }
            )

            if args.apply:
                tot_voided += await _apply_b1_dedup(session, tid, b1)
                tot_created += await _apply_b2_backfill(session, tid, b2)
                tot_recomputed += await _apply_b3_adjust(session, tid, b1["void_ids"])
            print()

        if args.out and report_rows:
            _write_report(args.out, report_rows)

        if args.apply:
            await session.commit()
            print(
                f"COMMIT: B1 voidó {tot_voided} movimiento(s), B2 creó {tot_created} gasto(s) "
                f"COGS, B3 ajustó el stock de {tot_recomputed} producto(s). "
                f"decision_type={_DECISION_TYPE}."
            )
        else:
            await session.rollback()
            print(
                "Dry-run: nada se escribió. Revisá el reporte (MEDIUM/LOW de B1 y los "
                "dudosos NO_COST/AMOUNT_MISMATCH de B2) antes de correr con --apply."
            )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
