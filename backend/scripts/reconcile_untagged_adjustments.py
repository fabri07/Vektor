"""Reconcilia movimientos 'adjustment' VIVOS sin ``source_type`` (procedencia) contra
los documentos y la auditoría del tenant, para desbloquear el CHECK
``ck_inventory_movements_adjustment_source_type`` (migración 20260728_0001).

CONTEXTO: hay ~655 movimientos ``adjustment`` con ``source_type IS NULL`` y
``voided_at IS NULL`` (caso real "don pedro", corrida masiva 2026-06-13, qty
negativa) y posiblemente más en otros tenants. El CHECK exige que todo adjustment
vivo tenga procedencia; estos la perdieron. Decisión del usuario: **cotejar cada uno
contra los documentos cargados; si coincide → backfillear ``source_type``; si no
coincide → anularlo (void + ajuste incremental de stock)**. Todo auditado y
reversible (ver ``revert_untagged_adjustment_reconciliation.py``).

CLASIFICACIÓN (primera regla que aplica gana — ver ``_classify_adjustment``):
    R2  MATCHED_BACKFILL_RECONCILIATION  el movimiento aparece en una reparación
        auditada (INVENTORY_REPAIR / …_FIX) → evidencia más fuerte. Solo backfillea
        ``source_type='reconciliation'``.
    R1  MATCHED_BACKFILL_CATALOG  el nombre/SKU del producto matchea en el bucket
        ``stock_detectado`` de EXACTAMENTE un upload dentro de la ventana temporal
        (``upload.created_at <= mv.created_at <= upload.created_at + window_hours``)
        y la qty es plausible → backfillea ``source_type='catalog_initial_stock'`` +
        ``source_upload_id`` + ``source_row_ref='reconciled:<idx>'``.
    R3  AMBIGUOUS_REPORT_ONLY  matchea en >1 upload, fuera de ventana, o con qty no
        plausible → SOLO reporte (nunca se toca): el producto SÍ aparece en
        documentos, así que jamás cae a VOID.
    R4  UNMATCHED_VOID  sin rastro en documentos ni auditoría → se anula (void) y se
        ajusta el stock del producto de forma INCREMENTAL.

REGLA DE STOCK: ``stock_units`` NO es Σ(ledger) — tiene base no-ledger (alta manual,
seed, catálogo que setea stock absoluto). Por eso el ajuste de VOID es INCREMENTAL
(``delta = -Σ(qty voideados)``; qty negativa ⇒ voidear SUBE el stock) y se siembra
``inventory_balances`` (INSERT con ``current_qty = stock_units`` actual) ANTES de
mutar ``stock_units`` — igual que ``stock_service.void_movement``.

REVERSA: la corrida completa (before/after de cada producto + items) queda en
``decision_audit_log`` (decision_type='UNTAGGED_ADJUSTMENT_RECONCILIATION'). Ese
before/after ES la fuente de la reversa. ``revert_untagged_adjustment_reconciliation.py``
la deshace — PERO solo es seguro ANTES de aplicar el CHECK: una vez aplicado, des-taggear
o des-voidear un adjustment sin procedencia lo dejaría vivo+sin source_type y violaría
la constraint. La reversa aborta si detecta el CHECK ya creado.

Todo dry-run por defecto; ``--apply`` escribe. SOLO SELECT en dry-run. NUNCA imprime
la connection URL (la provee el usuario desde su shell). Correr desde ``backend/``.

Usage:
    # Dry-run (default) de un tenant — revisar antes de aplicar
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/reconcile_untagged_adjustments.py \
        --tenant ee2625dc-96b7-464c-bda3-7f7018cc2a5b --out reconcile_report.csv

    # Dry-run global (todos los tenants activos)
    ... scripts/reconcile_untagged_adjustments.py --all-active --out reconcile_report.csv

    # Aplicar (después de revisar el dry-run)
    ... scripts/reconcile_untagged_adjustments.py --tenant <uuid> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import unicodedata
from datetime import timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import (  # noqa: E402
    REPAIR_DECISION_TYPES,
    async_engine_config,
    insert_decision_audit,
)
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_DECISION_TYPE = "UNTAGGED_ADJUSTMENT_RECONCILIATION"
_TRIGGERED_BY = "script:reconcile_untagged_adjustments"

# source_type canónicos (copia literal de valores de
# app/application/services/inventory_movement_origin.py — los scripts no importan el
# paquete `app` para no disparar la cadena config/ORM).
_SOURCE_CATALOG_INITIAL_STOCK = "catalog_initial_stock"
_SOURCE_RECONCILIATION = "reconciliation"

# Categorías de clasificación (van al CSV y a la auditoría).
_CAT_RECON = "MATCHED_BACKFILL_RECONCILIATION"
_CAT_CATALOG = "MATCHED_BACKFILL_CATALOG"
_CAT_AMBIGUOUS = "AMBIGUOUS_REPORT_ONLY"
_CAT_UNMATCHED = "UNMATCHED_VOID"

# decision_types de reparaciones auditadas cuyos movimientos, si aparecen, prueban
# procedencia de reconciliación (R2). Se buscan como substring del JSON serializado
# (mismo truco que diag_missing_purchases_scope.py). Superset del universo compartido
# (REPAIR_DECISION_TYPES) + dos tipos propios de este script.
_REPAIR_DECISION_TYPES = (
    *REPAIR_DECISION_TYPES,
    "ADJUSTMENT_SIGN_FIX",
    "COMPRAS_MERCADERIA_FIX",
)

# Claves de columna (normalizadas) donde puede venir el nombre / SKU / stock de una
# fila de ``stock_detectado``. Defensivo: los headers reales varían por planilla.
_NAME_KEYS = frozenset({"producto", "nombre", "descripcion", "articulo", "detalle"})
_SKU_KEYS = frozenset({"sku", "codigo", "cod", "inventario", "articulo", "item", "codigo_producto"})
_STOCK_KEYS = frozenset(
    {
        "stock",
        "stock_actual",
        "cantidad",
        "unidades",
        "existencia",
        "existencias",
        "mercaderia",
        "mercaderias",
        "inventario",
    }
)


def _norm_text(value: object) -> str:
    """Normaliza texto: sin tildes, minúsculas, trim, espacios colapsados.

    Copia literal de ``_norm_text`` en
    app/application/services/inventory_movement_origin.py — los scripts no importan el
    paquete ``app`` para no disparar la cadena de imports config/ORM.
    """
    if value is None:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.strip().lower().split())


def _num(value: object) -> float | None:
    """Normaliza un valor numérico (int/Decimal/str AR) a float; None/'' → None."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return float(value)
    s = str(value).strip().replace(" ", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    # Formato AR "1.234,50": punto=miles, coma=decimal.
    cleaned = s.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _int_or_float(value: object) -> int | float | None:
    """Devuelve int si el número es entero, si no float; para serializar la qty."""
    n = _num(value)
    if n is None:
        return None
    return int(n) if float(n).is_integer() else n


def _iter_stock_rows(parsed_summary: Any) -> list[dict[str, Any]]:
    """Filas del bucket ``stock_detectado`` del parsed_summary_json. Defensivo.

    El summary puede venir como dict o como str JSON (según cómo lo devuelva el driver).
    ``stock_detectado`` es el bucket canónico de filas de catálogo/stock detectadas por
    ``file_parsing.py`` (single-sheet, multi-hoja y texto/OCR comparten esta clave).
    """
    if isinstance(parsed_summary, str):
        try:
            parsed_summary = json.loads(parsed_summary)
        except (ValueError, TypeError):
            return []
    if not isinstance(parsed_summary, dict):
        return []
    rows = parsed_summary.get("stock_detectado")
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def _extract_row_fields(row: dict[str, Any]) -> tuple[str, str, float | None]:
    """De una fila cruda de ``stock_detectado`` extrae (nombre_norm, sku_norm, stock).

    Las claves son los headers reales (crudos, no normalizados) → se normaliza cada
    clave para buscarla en los sets. ``stock`` puede faltar (fila sin cantidad) → None.
    """
    name = ""
    sku = ""
    stock: float | None = None
    for raw_key, value in row.items():
        if raw_key == "__context__":
            continue
        nk = _norm_text(raw_key)
        if not name and nk in _NAME_KEYS and value not in (None, ""):
            name = _norm_text(value)
        if not sku and nk in _SKU_KEYS and value not in (None, ""):
            sku = _norm_text(value)
        if stock is None and nk in _STOCK_KEYS:
            stock = _num(value)
    return name, sku, stock


def _build_uploads_index(uploads: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Índice nombre/SKU normalizado → lista de entradas de stock documentadas.

    Cada entrada: ``{upload_id, upload_created_at, stock_fila, matched_on}``. Un mismo
    upload/fila indexa bajo su nombre Y su SKU (si tiene). ``stock_fila`` puede ser None.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for up in uploads:
        upload_id = str(up["id"])
        upload_created_at = up["created_at"]
        for row in _iter_stock_rows(up["parsed_summary_json"]):
            name, sku, stock = _extract_row_fields(row)
            for key, matched_on in ((name, "name"), (sku, "sku")):
                if not key:
                    continue
                index.setdefault(key, []).append(
                    {
                        "upload_id": upload_id,
                        "upload_created_at": upload_created_at,
                        "stock_fila": stock,
                        "matched_on": matched_on,
                    }
                )
    return index


def _qty_plausible(qty: float | None, stock_fila: float | None) -> bool:
    """qty plausible contra una fila de stock: ``qty == stock`` o ``abs(qty) <= stock``.

    Sin evidencia de qty (``stock_fila is None``) NO es plausible — el match de nombre
    solo, sin cantidad, no alcanza para R1 (cae a AMBIGUOUS, nunca a VOID).
    """
    if qty is None or stock_fila is None:
        return False
    if qty == stock_fila:
        return True
    return abs(qty) <= stock_fila


def _classify_adjustment(
    mv: dict[str, Any],
    uploads_index: dict[str, list[dict[str, Any]]],
    audit_movement_ids: set[str],
    window_hours: int,
) -> tuple[str, dict[str, Any]]:
    """Clasifica un adjustment sin procedencia. Pura y testeable. Primera regla gana.

    Devuelve ``(category, evidence)``. ``evidence`` justifica la categoría (va al CSV y
    a la auditoría).
    """
    # R2 — evidencia más fuerte: el movimiento fue creado/tocado por una reparación
    # auditada. Backfill 'reconciliation' (solo eso).
    if str(mv["id"]) in audit_movement_ids:
        return _CAT_RECON, {"audit_matched": True}

    name_key = _norm_text(mv.get("name"))
    sku_key = _norm_text(mv.get("sku"))
    entries: list[dict[str, Any]] = []
    if name_key:
        entries.extend(uploads_index.get(name_key, []))
    if sku_key and sku_key != name_key:
        entries.extend(uploads_index.get(sku_key, []))

    if not entries:
        # R4 — sin rastro en documentos ni auditoría → void.
        return _CAT_UNMATCHED, {"reason": "no_document_or_audit_match"}

    # El producto APARECE en documentos → nunca VOID desde acá. R1 o R3.
    qty = _num(mv.get("qty"))
    mv_created = mv.get("created_at")

    in_window: list[dict[str, Any]] = []
    if mv_created is not None:
        window = timedelta(hours=window_hours)
        for e in entries:
            uc = e.get("upload_created_at")
            if uc is not None and uc <= mv_created <= uc + window:
                in_window.append(e)

    uploads_in_window = sorted({e["upload_id"] for e in in_window})
    plausible = next((e for e in in_window if _qty_plausible(qty, e.get("stock_fila"))), None)

    if len(uploads_in_window) == 1 and plausible is not None:
        # R1 — exactamente un upload en ventana + qty plausible → catalog.
        idx = entries.index(plausible)
        return _CAT_CATALOG, {
            "upload_id": plausible["upload_id"],
            "stock_fila": plausible.get("stock_fila"),
            "matched_on": plausible.get("matched_on"),
            "row_ref": f"reconciled:{idx}",
            "window_hours": window_hours,
        }

    # R3 — matchea (>1 upload, fuera de ventana, o qty no plausible) → solo reporte.
    if not uploads_in_window:
        reason = "matched_out_of_window"
    elif len(uploads_in_window) > 1:
        reason = "matched_multiple_uploads"
    else:
        reason = "qty_not_plausible"
    return _CAT_AMBIGUOUS, {
        "reason": reason,
        "upload_ids": sorted({e["upload_id"] for e in entries}),
        "uploads_in_window": uploads_in_window,
        "stock_filas": [e.get("stock_fila") for e in (in_window or entries)],
    }


async def _plan_tenant(
    session: AsyncSession, tid: str, window_hours: int
) -> list[dict[str, Any]]:
    """Plan de reconciliación del tenant (read-only). Una fila por adjustment objetivo."""
    adjustments = (
        await session.execute(
            text(
                "SELECT im.id, im.product_id, p.name, p.sku, p.stock_units, "
                "im.qty, im.unit_cost, im.created_at "
                "FROM inventory_movements im "
                "JOIN products p ON p.id = im.product_id AND p.tenant_id = im.tenant_id "
                "WHERE im.tenant_id = CAST(:tid AS uuid) "
                "AND im.movement_type = 'adjustment' "
                "AND im.source_type IS NULL AND im.voided_at IS NULL"
            ),
            {"tid": tid},
        )
    ).mappings().all()
    if not adjustments:
        return []

    uploads = (
        await session.execute(
            text(
                "SELECT id, created_at, parsed_summary_json FROM uploaded_files "
                "WHERE tenant_id = CAST(:tid AS uuid) AND parsed_summary_json IS NOT NULL"
            ),
            {"tid": tid},
        )
    ).mappings().all()
    uploads_index = _build_uploads_index([dict(u) for u in uploads])

    audit_rows = (
        await session.execute(
            text(
                "SELECT decision_data FROM decision_audit_log "
                "WHERE tenant_id = CAST(:tid AS uuid) AND decision_type = ANY(:types)"
            ),
            {"tid": tid, "types": list(_REPAIR_DECISION_TYPES)},
        )
    ).mappings().all()
    audit_blobs = [
        dd if isinstance((dd := r["decision_data"]), str) else json.dumps(dd) for r in audit_rows
    ]
    audit_movement_ids: set[str] = {
        str(mv["id"]) for mv in adjustments if any(str(mv["id"]) in blob for blob in audit_blobs)
    }

    items: list[dict[str, Any]] = []
    for mv in adjustments:
        mvd = dict(mv)
        category, evidence = _classify_adjustment(
            mvd, uploads_index, audit_movement_ids, window_hours
        )
        backfill: dict[str, Any] | None = None
        voided = False
        if category == _CAT_RECON:
            backfill = {
                "source_type": _SOURCE_RECONCILIATION,
                "source_upload_id": None,
                "source_row_ref": None,
            }
        elif category == _CAT_CATALOG:
            backfill = {
                "source_type": _SOURCE_CATALOG_INITIAL_STOCK,
                "source_upload_id": evidence["upload_id"],
                "source_row_ref": evidence["row_ref"],
            }
        elif category == _CAT_UNMATCHED:
            voided = True
        items.append(
            {
                "tenant_id": tid,
                "movement_id": str(mvd["id"]),
                "product_id": str(mvd["product_id"]),
                "product_name": mvd.get("name"),
                "qty": _int_or_float(mvd.get("qty")),
                "unit_cost": (None if mvd.get("unit_cost") is None else float(mvd["unit_cost"])),
                "mv_created_at": (
                    mvd["created_at"].isoformat() if mvd.get("created_at") is not None else None
                ),
                "category": category,
                "evidence": evidence,
                "backfill": backfill,
                "voided": voided,
            }
        )
    return items


def _compute_stock_after(stock_before: int, delta: float) -> tuple[int, bool]:
    """Calcula el stock resultante de un ajuste incremental, clampeado a 0.

    Pura y testeable — reusada por la mutación real (``_apply_tenant``) y por el
    plan read-only de dry-run (``_plan_stock_changes``), para no duplicar la
    aritmética del clamp en dos lugares.
    """
    raw_after = stock_before + delta
    stock_after = max(0, raw_after)
    clamped = raw_after < 0
    return stock_after, clamped


def _group_voids_by_product(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Agrupa los items con VOID planificado por producto (para el delta incremental)."""
    voids_by_product: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        if it["voided"]:
            voids_by_product.setdefault(it["product_id"], []).append(it)
    return voids_by_product


async def _plan_stock_changes(
    session: AsyncSession, tid: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Plan de stock (read-only) para los VOID planificados — usado en dry-run.

    Mismo cálculo que ``_apply_tenant`` (delta incremental + clamp), pero SOLO
    SELECT: no muta ``products`` ni ``inventory_balances``. Permite mostrar
    stock_before/stock_after/clamped ANTES de aplicar.
    """
    stock_changes: list[dict[str, Any]] = []
    for pid, prod_items in _group_voids_by_product(items).items():
        delta = -sum((it["qty"] or 0) for it in prod_items)
        stock_before = (
            await session.execute(
                text(
                    "SELECT stock_units FROM products "
                    "WHERE id = CAST(:pid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"pid": pid, "tid": tid},
            )
        ).scalar_one()
        stock_before = int(stock_before)
        stock_after, clamped = _compute_stock_after(stock_before, delta)
        stock_changes.append(
            {
                "product_id": pid,
                "stock_before": stock_before,
                "stock_after": stock_after,
                "clamped": clamped,
            }
        )
    return stock_changes


async def _apply_tenant(
    session: AsyncSession, tid: str, items: list[dict[str, Any]], window_hours: int
) -> dict[str, Any]:
    """Aplica backfills y voids del tenant. Reversible y auditado.

    Devuelve un dict de resumen (backfilled, voided, stock_changes).
    """
    backfilled = 0
    voided = 0

    # 1. Backfills (idempotentes: el WHERE re-chequea el estado NULL/vivo).
    for it in items:
        bf = it["backfill"]
        if bf is None:
            continue
        await session.execute(
            text(
                "UPDATE inventory_movements "
                "SET source_type = :st, source_upload_id = :uid, source_row_ref = :ref "
                "WHERE id = CAST(:mid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND voided_at IS NULL AND source_type IS NULL"
            ),
            {
                "st": bf["source_type"],
                "uid": bf["source_upload_id"],
                "ref": bf["source_row_ref"],
                "mid": it["movement_id"],
                "tid": tid,
            },
        )
        backfilled += 1

    # 2. Voids, agrupados por producto para el ajuste incremental de stock.
    voids_by_product = _group_voids_by_product(items)

    stock_changes: list[dict[str, Any]] = []
    for pid, prod_items in voids_by_product.items():
        # delta = -Σ(qty voideados): qty negativa ⇒ voidear SUBE el stock.
        delta = -sum((it["qty"] or 0) for it in prod_items)

        for it in prod_items:
            await session.execute(
                text(
                    "UPDATE inventory_movements SET voided_at = now() "
                    "WHERE id = CAST(:mid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                    "AND voided_at IS NULL"
                ),
                {"mid": it["movement_id"], "tid": tid},
            )
            voided += 1

        # Stock actual (before) + sembrar inventory_balances ANTES de mutar stock_units.
        stock_before = (
            await session.execute(
                text(
                    "SELECT stock_units FROM products "
                    "WHERE id = CAST(:pid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
                ),
                {"pid": pid, "tid": tid},
            )
        ).scalar_one()
        stock_before = int(stock_before)

        existing_balance = (
            await session.execute(
                text(
                    "SELECT id FROM inventory_balances "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND product_id = CAST(:pid AS uuid)"
                ),
                {"tid": tid, "pid": pid},
            )
        ).first()
        if existing_balance is None:
            await session.execute(
                text(
                    "INSERT INTO inventory_balances "
                    "(id, tenant_id, product_id, current_qty, reserved_qty, "
                    "created_at, updated_at) "
                    "VALUES (gen_random_uuid(), CAST(:tid AS uuid), CAST(:pid AS uuid), "
                    ":cq, 0, now(), now())"
                ),
                {"tid": tid, "pid": pid, "cq": stock_before},
            )

        stock_after, clamped = _compute_stock_after(stock_before, delta)

        await session.execute(
            text(
                "UPDATE products SET stock_units = GREATEST(0, stock_units + :delta) "
                "WHERE id = CAST(:pid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"delta": delta, "pid": pid, "tid": tid},
        )
        await session.execute(
            text(
                "UPDATE inventory_balances "
                "SET current_qty = GREATEST(0, current_qty + :delta) "
                "WHERE tenant_id = CAST(:tid AS uuid) AND product_id = CAST(:pid AS uuid)"
            ),
            {"delta": delta, "pid": pid, "tid": tid},
        )
        stock_changes.append(
            {
                "product_id": pid,
                "stock_before": stock_before,
                "stock_after": stock_after,
                "clamped": clamped,
            }
        )

    # 3. Auditoría — UN insert por tenant con cambios. El before/after ES la reversa.
    changed_items = [it for it in items if it["backfill"] is not None or it["voided"]]
    if changed_items or stock_changes:
        counts = _counts_by_category(items)
        decision_data = {
            "mode": "apply",
            "window_hours": window_hours,
            "counts_by_category": counts,
            "items": [
                {
                    "movement_id": it["movement_id"],
                    "product_id": it["product_id"],
                    "category": it["category"],
                    "backfill": it["backfill"],
                    "voided": it["voided"],
                    "qty": it["qty"],
                }
                for it in changed_items
            ],
            "stock_changes": stock_changes,
        }
        await insert_decision_audit(
            session,
            tenant_id=tid,
            decision_type=_DECISION_TYPE,
            decision_data=decision_data,
            triggered_by=_TRIGGERED_BY,
        )
    return {"backfilled": backfilled, "voided": voided, "stock_changes": stock_changes}


def _counts_by_category(items: list[dict[str, Any]]) -> dict[str, int]:
    from collections import Counter

    return dict(Counter(it["category"] for it in items))


def _annotate_stock_changes(
    items: list[dict[str, Any]], stock_changes: list[dict[str, Any]]
) -> None:
    """Adjunta stock_before/stock_after/clamped (del plan de su producto) a cada item
    VOID, in-place. Items no-VOID quedan con esos campos en ``None`` (CSV vacío)."""
    by_product = {sc["product_id"]: sc for sc in stock_changes}
    for it in items:
        sc = by_product.get(it["product_id"]) if it["voided"] else None
        it["stock_before"] = sc["stock_before"] if sc else None
        it["stock_after"] = sc["stock_after"] if sc else None
        it["clamped"] = sc["clamped"] if sc else None


def _print_summary(
    tid: str, items: list[dict[str, Any]], stock_changes: list[dict[str, Any]] | None = None
) -> None:
    counts = _counts_by_category(items)
    products = {it["product_id"] for it in items if it["voided"]}
    print(f"tenant {tid}: {len(items)} adjustment(s) sin procedencia — {counts}")
    if products:
        print(f"    {len(products)} producto(s) con VOID (ajuste incremental de stock)")
    for sc in stock_changes or []:
        flag = " [CLAMP]" if sc["clamped"] else ""
        print(
            f"      producto {sc['product_id']}: stock {sc['stock_before']} -> "
            f"{sc['stock_after']}{flag}"
        )


def _csv_or_blank(value: Any) -> Any:
    return "" if value is None else value


def _write_report(path: str, items: list[dict[str, Any]]) -> None:
    fieldnames = [
        "tenant_id",
        "movement_id",
        "product_id",
        "product_name",
        "qty",
        "unit_cost",
        "mv_created_at",
        "category",
        "action",
        "evidence_json",
        "stock_before",
        "stock_after",
        "clamped",
    ]
    action_by_cat = {
        _CAT_RECON: "backfill_reconciliation",
        _CAT_CATALOG: "backfill_catalog",
        _CAT_AMBIGUOUS: "report_only",
        _CAT_UNMATCHED: "void",
    }
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for it in items:
                writer.writerow(
                    {
                        "tenant_id": it["tenant_id"],
                        "movement_id": it["movement_id"],
                        "product_id": it["product_id"],
                        "product_name": it["product_name"],
                        "qty": it["qty"],
                        "unit_cost": it["unit_cost"],
                        "mv_created_at": it["mv_created_at"],
                        "category": it["category"],
                        "action": action_by_cat.get(it["category"], ""),
                        "evidence_json": json.dumps(it["evidence"], ensure_ascii=False),
                        "stock_before": _csv_or_blank(it.get("stock_before")),
                        "stock_after": _csv_or_blank(it.get("stock_after")),
                        "clamped": _csv_or_blank(it.get("clamped")),
                    }
                )
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False, indent=2, default=str)
    print(f"\nReporte escrito en {path} ({len(items)} fila(s)).")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcilia adjustments sin procedencia.")
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument("--out", default="reconcile_report.csv", help="Path del reporte (CSV/JSON)")
    parser.add_argument(
        "--window-hours",
        type=int,
        default=48,
        help="Ventana upload.created_at → mv.created_at para R1 (default 48h)",
    )
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: indicá --tenant <uuid> o --all-active.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        if args.tenant:
            tids = [args.tenant]
        else:
            rows = await session.execute(
                text(
                    # Canónico uppercase: la app solo escribe 'ACTIVE'/'TRIAL' (ver deps.py).
                    "SELECT tenant_id FROM tenants WHERE status IN ('ACTIVE', 'TRIAL')"
                )
            )
            tids = [str(r[0]) for r in rows.all()]

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] reconciliación de adjustments sin procedencia en {len(tids)} tenant(s) "
              f"(ventana={args.window_hours}h)\n")

        all_items: list[dict[str, Any]] = []
        total_backfilled = 0
        total_voided = 0
        for tid in tids:
            items = await _plan_tenant(session, tid, args.window_hours)
            if not items:
                continue
            if args.apply:
                res = await _apply_tenant(session, tid, items, args.window_hours)
                total_backfilled += res["backfilled"]
                total_voided += res["voided"]
                stock_changes = res["stock_changes"]
            else:
                # Dry-run: mismo plan de stock (read-only), para dar visibilidad de
                # clamps ANTES de aplicar.
                stock_changes = await _plan_stock_changes(session, tid, items)
            _annotate_stock_changes(items, stock_changes)
            all_items.extend(items)
            _print_summary(tid, items, stock_changes)
            print()

        if args.out:
            _write_report(args.out, all_items)

        if args.apply:
            await session.commit()
            print(
                f"COMMIT: {total_backfilled} backfill(s) + {total_voided} void(s) "
                f"(decision_type={_DECISION_TYPE}). Reversa: "
                f"revert_untagged_adjustment_reconciliation.py (solo ANTES del CHECK)."
            )
        else:
            await session.rollback()
            counts = _counts_by_category(all_items)
            print(f"Dry-run: nada se escribió. Con --apply se aplicaría: {counts}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
