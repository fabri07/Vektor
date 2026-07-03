"""Desactiva proveedores que en realidad son MARCAS creadas por import de catálogo.

Sistémico, reversible, dry-run por defecto. Detecta proveedores fantasma —los que
un import de catálogo de productos creó por cada valor de la columna "Tienda"/marca,
sin email ni teléfono— y los soft-deletea, devolviendo la marca a su lugar correcto:
un atributo del producto en ``Product.custom_fields["marca"]``.

HEURÍSTICA (todos los AND):
    suppliers.email IS NULL AND suppliers.phone IS NULL
    Y existen >= 2 Products del MISMO tenant con custom_fields->>'marca' = suppliers.name
      (fallback: custom_fields->>'proveedor' = suppliers.name, datos viejos pre-rename).
      Se exige >= 2 productos para no confundir una marca real de catálogo (varios SKUs)
      con un proveedor real cuyo nombre coincide con la marca de un único producto.
    Y NO es el sentinela (custom_fields->>'_sentinel' = 'true')
    Y el nombre no es "No identificado".

CLASIFICACIÓN (motivo por candidato, en el reporte --out):
    BRAND_FROM_CATALOG  → matchea marca de catálogo (>=2 productos) y no tiene gastos
                          reales → auto-aplicable (desactiva; la marca ya está en el producto).
    MERCH_SOURCE_NO_CONTACT → sin email/phone, NO creado en la app, sin OPEX, referenciado
                          SOLO por compras de mercadería (COGS) → es marca/origen, no
                          proveedor. AUTO-APLICABLE: re-apunta esos gastos al sentinela
                          "No identificado", mueve la marca al producto comprado
                          (custom_fields["marca"]) y desactiva la marca-proveedor.
    NO_EXPENSES         → sin email/phone, sin gastos, pero NO matchea marca → informativo,
                          NO se desactiva (no hay señal fuerte de que vino de catálogo).
    REVIEW_MANUAL       → señal de proveedor real: creado/editado en la app (audit log
                          DATA_RECORD_CREATED/UPDATED) o referenciado por gastos OPEX
                          (le facturan) → NO se desactiva; queda para revisión humana.
    EXCLUDED_SENTINEL   → es el sentinela "No identificado" → nunca se toca.

Con --apply se desactivan los BRAND_FROM_CATALOG y los MERCH_SOURCE_NO_CONTACT.

Usage:
    # Dry-run (default) de un tenant: lista candidatos y motivos, no escribe nada.
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/deactivate_brand_suppliers.py \
        --tenant <uuid>

    # Dry-run global (todos los tenants activos):
    ... scripts/deactivate_brand_suppliers.py --all-active

    # Escribir el reporte a un archivo (CSV por extensión .csv, si no JSON):
    ... scripts/deactivate_brand_suppliers.py --tenant <uuid> --out reporte.csv

    # Aplicar (después de revisar el dry-run):
    ... scripts/deactivate_brand_suppliers.py --tenant <uuid> --apply

    # Aplicar y reasignar los productos de esas marcas al sentinela "No identificado":
    ... scripts/deactivate_brand_suppliers.py --tenant <uuid> --apply --reassign-to-sentinel

═══════════════════════════════════════════════════════════════════════════════
CÓMO REVERTIR EL CLEANUP
═══════════════════════════════════════════════════════════════════════════════
Todo cambio queda trazado en decision_audit_log (decision_type='SUPPLIER_BRAND_CLEANUP',
decision_data con before/after y el supplier_id/product_ids afectados). Para revertir:

  1. Reactivar el proveedor (deja de estar soft-deleted):
       UPDATE suppliers SET deactivated_at = NULL WHERE id = '<supplier_id>';

  2. Restaurar la marca en los productos (el valor previo quedó en '_marca_prev'):
       UPDATE products
         SET custom_fields = jsonb_set(
               custom_fields - '_marca_prev',
               '{marca}', custom_fields->'_marca_prev')
         WHERE tenant_id = '<tid>' AND custom_fields ? '_marca_prev';

  3. Si se usó --reassign-to-sentinel, los productos quedaron además con
     custom_fields->>'_reassigned_from' = '<supplier_id viejo>'. La reasignación de
     supplier en este script NO toca productos (los productos no referencian supplier
     directo); el flag '_reassigned_from' es solo traza informativa y puede limpiarse:
       UPDATE products SET custom_fields = custom_fields - '_reassigned_from'
         WHERE tenant_id='<tid>' AND custom_fields ? '_reassigned_from';

REVERSA DE MERCH_SOURCE_NO_CONTACT (colapso a "No identificado"):
  El supplier_id previo de cada gasto quedó en expense_entries.custom_fields["_supplier_prev"].
  a. Reactivar la marca-proveedor: UPDATE suppliers SET deactivated_at=NULL WHERE id='<sid>';
  b. Devolver los gastos a su proveedor original:
       UPDATE expense_entries
         SET supplier_id = CAST(custom_fields->>'_supplier_prev' AS uuid),
             custom_fields = custom_fields - '_supplier_prev'
         WHERE tenant_id='<tid>' AND custom_fields->>'_supplier_prev' = '<sid>';
  c. La marca quedó en products.custom_fields["marca"] (con el valor previo en
     '_marca_prev' si lo había). Restaurala/quitala según corresponda.
  d. Los movimientos de compra (inventory_movements) de esos productos quedaron con
     supplier_id = sentinela. Reversa: UPDATE inventory_movements SET supplier_id=NULL
     WHERE tenant_id='<tid>' AND supplier_id='<sentinel_id>' (ver collapsed_to_sentinel).
  Cada colapso está en decision_audit_log con decision_type='SUPPLIER_MERCH_SOURCE_COLLAPSE'
  (expense_ids/product_ids/movements_attributed/collapsed_to_sentinel en decision_data).

Buscá la traza completa en:
    SELECT * FROM decision_audit_log
      WHERE tenant_id='<tid>'
        AND decision_type IN ('SUPPLIER_BRAND_CLEANUP','SUPPLIER_MERCH_SOURCE_COLLAPSE')
      ORDER BY created_at DESC;

NUNCA borra filas ni imprime la connection URL. Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_SENTINEL_NAME = "No identificado"
_DECISION_TYPE = "SUPPLIER_BRAND_CLEANUP"
_DECISION_TYPE_MERCH = "SUPPLIER_MERCH_SOURCE_COLLAPSE"

# Motivos de clasificación (van al reporte y a la auditoría).
_BRAND_FROM_CATALOG = "BRAND_FROM_CATALOG"
# Marca/origen atado SOLO a compras de mercadería (COGS), sin datos de contacto y
# sin creación en la app: no es un proveedor verificado. Se colapsa al sentinela
# "No identificado" (re-apuntando los gastos) y la marca pasa al producto.
_MERCH_SOURCE = "MERCH_SOURCE_NO_CONTACT"
_NO_EXPENSES = "NO_EXPENSES"
_REVIEW_MANUAL = "REVIEW_MANUAL"
_EXCLUDED_SENTINEL = "EXCLUDED_SENTINEL"
# Reactivado por revert_brand_supplier_collapse.py (flag _provisional_from_brand):
# decisión humana de restaurar la marca-proveedor — este script no la vuelve a tocar.
_EXCLUDED_PROVISIONAL = "EXCLUDED_PROVISIONAL"


async def _resolve_or_create_sentinel(session: AsyncSession, tid: uuid.UUID) -> uuid.UUID:
    """Devuelve el id del sentinela "No identificado" del tenant; lo crea si no existe.

    Solo se llama bajo --apply (escribe). El flag ``_sentinel`` se guarda como string
    "true" (mismo criterio que la app).
    """
    row = (
        await session.execute(
            text(
                "SELECT id FROM suppliers "
                "WHERE tenant_id = :tid AND deactivated_at IS NULL "
                "AND custom_fields->>'_sentinel' = 'true' LIMIT 1"
            ),
            {"tid": tid},
        )
    ).first()
    if row is not None:
        existing_id: uuid.UUID = row[0]
        return existing_id
    new_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO suppliers (id, tenant_id, name, custom_fields, created_at, updated_at) "
            "VALUES (:id, :tid, :name, CAST(:cf AS jsonb), now(), now())"
        ),
        {
            "id": new_id,
            "tid": tid,
            "name": _SENTINEL_NAME,
            "cf": json.dumps({"_sentinel": "true"}),
        },
    )
    return new_id


async def _classify_tenant(
    session: AsyncSession, tid: uuid.UUID
) -> list[dict[str, Any]]:
    """Devuelve una fila por proveedor candidato con su motivo de clasificación.

    Sin escribir nada. Un proveedor entra a la lista si tiene email/phone vacíos
    (señal de fantasma de catálogo); el motivo lo decide la combinación de
    matcheo-de-marca, gastos reales y señales manuales.
    """
    suppliers = (
        await session.execute(
            text(
                "SELECT id, name, email, phone, custom_fields, created_at, updated_at "
                "FROM suppliers "
                "WHERE tenant_id = :tid AND deactivated_at IS NULL "
                "AND email IS NULL AND phone IS NULL "
                "ORDER BY name"
            ),
            {"tid": tid},
        )
    ).mappings().all()

    candidates: list[dict[str, Any]] = []
    for s in suppliers:
        cf = s["custom_fields"] or {}
        if isinstance(cf, str):
            cf = json.loads(cf)
        name = s["name"]

        # Excluir el sentinela: nunca se toca. El flag puede ser string "true" o
        # booleano JSON true (mismo criterio que la app, app/persistence/models/supplier.py).
        if cf.get("_sentinel") in (True, "true") or name == _SENTINEL_NAME:
            candidates.append(_row(tid, s, _EXCLUDED_SENTINEL, matches_brand=False))
            continue

        # Excluir proveedores provisionales reactivados por
        # revert_brand_supplier_collapse.py: son marcas-proveedor que un humano decidió
        # RESTAURAR (el colapso se revirtió a propósito). Sin esta guarda, re-correr
        # este script después del revert los volvería a colapsar (ping-pong), y peor:
        # los movimientos que el revert re-apuntó (ya no NULL) no se moverían al
        # sentinela, dejando mercadería colgada de un proveedor desactivado.
        if cf.get("_provisional_from_brand") in (True, "true"):
            candidates.append(_row(tid, s, _EXCLUDED_PROVISIONAL, matches_brand=False))
            continue

        # ¿Cuántos productos de este tenant tienen esta marca/proveedor? Una marca real
        # de catálogo abarca VARIOS productos; exigir >= 2 evita el falso positivo de un
        # proveedor real (sin email/phone) cuyo nombre coincide con la marca de UN solo
        # producto. Las coincidencias de 1 producto caen a NO_EXPENSES (no se desactivan).
        brand_count = (
            await session.execute(
                text(
                    "SELECT COUNT(DISTINCT id) FROM products "
                    "WHERE tenant_id = :tid AND deactivated_at IS NULL "
                    "AND (custom_fields->>'marca' = :name "
                    "     OR custom_fields->>'proveedor' = :name)"
                ),
                {"tid": tid, "name": name},
            )
        ).scalar() or 0
        matches_brand = brand_count >= 2

        # Gastos que referencian a este proveedor, desglosados por tipo. Un proveedor
        # REAL eventualmente aparece en OPEX (te factura servicios, le pagás); una
        # MARCA solo aparece en COGS (compras de mercadería).
        type_rows = (
            await session.execute(
                text(
                    "SELECT expense_type, COUNT(*) AS n FROM expense_entries "
                    "WHERE tenant_id = :tid AND supplier_id = :sid "
                    "AND voided_at IS NULL GROUP BY expense_type"
                ),
                {"tid": tid, "sid": s["id"]},
            )
        ).mappings().all()
        by_type = {r["expense_type"]: r["n"] for r in type_rows}
        has_cogs = by_type.get("COGS", 0) > 0
        has_opex = by_type.get("OPEX", 0) > 0

        # Señales de creación/edición manual. El router (app/api/v1/suppliers.py)
        # audita con decision_type genéricos DATA_RECORD_CREATED/UPDATED (POST/PATCH
        # /suppliers) — los usan también otras entidades, por eso hay que filtrar por
        # record_type='supplier' + record_id.
        manual_audit = (
            await session.execute(
                text(
                    "SELECT 1 FROM decision_audit_log "
                    "WHERE tenant_id = :tid "
                    "AND decision_type IN ('DATA_RECORD_CREATED','DATA_RECORD_UPDATED') "
                    "AND decision_data->>'record_type' = 'supplier' "
                    "AND decision_data->>'record_id' = :sid LIMIT 1"
                ),
                {"tid": tid, "sid": str(s["id"])},
            )
        ).first() is not None
        edited_after_create = (
            s["updated_at"] is not None
            and s["created_at"] is not None
            and s["updated_at"] > s["created_at"]
        )

        # Decisión (orden = prioridad de las señales más fuertes a las más débiles).
        if manual_audit:
            reason = _REVIEW_MANUAL  # creado/editado en la app (POST/PATCH) → real
        elif has_opex:
            reason = _REVIEW_MANUAL  # le facturás OPEX → proveedor real, no marca
        elif matches_brand:
            reason = _BRAND_FROM_CATALOG  # marca de catálogo (>=2 productos)
        elif has_cogs:
            # Sin contacto, sin creación en la app, sin OPEX, referenciado SOLO por
            # compras de mercadería → marca/origen, no proveedor. Colapsar al sentinela.
            reason = _MERCH_SOURCE
        elif edited_after_create:
            # Señal débil (un proceso de fondo tocó la fila) y sin otra info → revisión.
            reason = _REVIEW_MANUAL
        else:
            reason = _NO_EXPENSES  # fantasma sin matcheo ni gastos → informativo

        candidates.append(_row(tid, s, reason, matches_brand=matches_brand))
    return candidates


def _row(
    tid: uuid.UUID, s: Any, reason: str, *, matches_brand: bool
) -> dict[str, Any]:
    return {
        "tenant_id": str(tid),
        "supplier_id": str(s["id"]),
        "name": s["name"],
        "reason": reason,
        "matches_brand": matches_brand,
    }


async def _apply_tenant(
    session: AsyncSession,
    tid: uuid.UUID,
    candidates: list[dict[str, Any]],
    *,
    reassign_to_sentinel: bool,
) -> int:
    """Desactiva los candidatos BRAND_FROM_CATALOG. Reversible y auditado."""
    sentinel_id: uuid.UUID | None = None
    if reassign_to_sentinel:
        row = (
            await session.execute(
                text(
                    "SELECT id FROM suppliers "
                    "WHERE tenant_id = :tid AND deactivated_at IS NULL "
                    "AND custom_fields->>'_sentinel' = 'true' LIMIT 1"
                ),
                {"tid": tid},
            )
        ).first()
        if row is None:
            print(
                f"  ⚠ tenant {tid}: --reassign-to-sentinel pedido pero no existe el "
                f"sentinela '{_SENTINEL_NAME}'. NO se crea acá — reasignación omitida."
            )
        else:
            sentinel_id = row[0]

    applied = 0
    for c in candidates:
        if c["reason"] != _BRAND_FROM_CATALOG:
            continue
        sid = uuid.UUID(c["supplier_id"])
        name = c["name"]

        # Productos cuya marca apunta a este proveedor (para mover el valor a _marca_prev).
        prods = (
            await session.execute(
                text(
                    "SELECT id, custom_fields FROM products "
                    "WHERE tenant_id = :tid AND deactivated_at IS NULL "
                    "AND (custom_fields->>'marca' = :name "
                    "     OR custom_fields->>'proveedor' = :name)"
                ),
                {"tid": tid, "name": name},
            )
        ).mappings().all()

        product_ids: list[str] = []
        for p in prods:
            pcf = p["custom_fields"] or {}
            if isinstance(pcf, str):
                pcf = json.loads(pcf)
            prev = pcf.get("marca", pcf.get("proveedor"))
            new_cf = dict(pcf)
            if prev is not None:
                new_cf["_marca_prev"] = prev
            new_cf.pop("marca", None)
            new_cf.pop("proveedor", None)
            if reassign_to_sentinel and sentinel_id is not None:
                new_cf["_reassigned_from"] = str(sid)
            await session.execute(
                text(
                    "UPDATE products SET custom_fields = CAST(:cf AS jsonb) "
                    "WHERE id = :pid AND tenant_id = :tid"
                ),
                {"cf": json.dumps(new_cf), "pid": p["id"], "tid": tid},
            )
            product_ids.append(str(p["id"]))

        # Soft-delete del proveedor.
        await session.execute(
            text(
                "UPDATE suppliers SET deactivated_at = now() "
                "WHERE id = :sid AND tenant_id = :tid"
            ),
            {"sid": sid, "tid": tid},
        )

        # Auditoría (insert-only). created_at NOT NULL sin default → now() explícito.
        decision_data = {
            "supplier_id": str(sid),
            "supplier_name": name,
            "before": {"deactivated_at": None},
            "after": {"deactivated_at": "now()"},
            "product_ids": product_ids,
            "reassigned_to_sentinel": str(sentinel_id) if sentinel_id else None,
        }
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
                "tb": "script:deactivate_brand_suppliers",
            },
        )
        applied += 1
    return applied


async def _apply_merch_source(
    session: AsyncSession, tid: uuid.UUID, candidates: list[dict[str, Any]]
) -> int:
    """Colapsa los MERCH_SOURCE_NO_CONTACT al sentinela "No identificado".

    Por cada marca: re-apunta sus gastos COGS al sentinela (guardando el supplier_id
    previo en expense.custom_fields["_supplier_prev"]), mueve el nombre de la marca a
    Product.custom_fields["marca"] del producto comprado (preservando un valor previo
    en "_marca_prev"), y desactiva la marca-proveedor. Reversible y auditado.
    """
    merch = [c for c in candidates if c["reason"] == _MERCH_SOURCE]
    if not merch:
        return 0
    sentinel_id = await _resolve_or_create_sentinel(session, tid)

    applied = 0
    for c in merch:
        sid = uuid.UUID(c["supplier_id"])
        name = c["name"]

        # Gastos que apuntan a esta marca (con su producto, si lo tienen).
        exps = (
            await session.execute(
                text(
                    "SELECT id, product_id, custom_fields FROM expense_entries "
                    "WHERE tenant_id = :tid AND supplier_id = :sid AND voided_at IS NULL"
                ),
                {"tid": tid, "sid": sid},
            )
        ).mappings().all()

        expense_ids: list[str] = []
        product_ids: list[str] = []
        movements_attributed = 0
        for e in exps:
            ecf = e["custom_fields"] or {}
            if isinstance(ecf, str):
                ecf = json.loads(ecf)
            new_ecf = dict(ecf)
            new_ecf["_supplier_prev"] = str(sid)  # reversibilidad
            # Mover la marca al producto comprado (si el gasto tiene product_id).
            if e["product_id"] is not None:
                prow = (
                    await session.execute(
                        text(
                            "SELECT custom_fields FROM products "
                            "WHERE id = :pid AND tenant_id = :tid"
                        ),
                        {"pid": e["product_id"], "tid": tid},
                    )
                ).first()
                if prow is not None:
                    pcf = prow[0] or {}
                    if isinstance(pcf, str):
                        pcf = json.loads(pcf)
                    new_pcf = dict(pcf)
                    existing = new_pcf.get("marca")
                    if existing is not None and existing != name:
                        new_pcf["_marca_prev"] = existing
                    new_pcf["marca"] = name
                    await session.execute(
                        text(
                            "UPDATE products SET custom_fields = CAST(:cf AS jsonb) "
                            "WHERE id = :pid AND tenant_id = :tid"
                        ),
                        {"cf": json.dumps(new_pcf), "pid": e["product_id"], "tid": tid},
                    )
                    product_ids.append(str(e["product_id"]))
                    # Atribuir los movimientos de compra (sin proveedor) de ese producto
                    # al sentinela, para que la tabla "productos comprados" (Fase 3, que
                    # lee inventory_movements.supplier_id) muestre la mercadería bajo
                    # "No identificado". Solo toca los que están NULL (reversa = NULL).
                    mres = await session.execute(
                        text(
                            "UPDATE inventory_movements SET supplier_id = :sentinel "
                            "WHERE tenant_id = :tid AND product_id = :pid "
                            "AND movement_type = 'purchase' AND supplier_id IS NULL"
                        ),
                        {"sentinel": sentinel_id, "tid": tid, "pid": e["product_id"]},
                    )
                    movements_attributed += getattr(mres, "rowcount", 0) or 0
            # Re-apuntar el gasto al sentinela.
            await session.execute(
                text(
                    "UPDATE expense_entries "
                    "SET supplier_id = :sentinel, supplier_name = :sname, "
                    "    custom_fields = CAST(:cf AS jsonb) "
                    "WHERE id = :eid AND tenant_id = :tid"
                ),
                {
                    "sentinel": sentinel_id,
                    "sname": _SENTINEL_NAME,
                    "cf": json.dumps(new_ecf),
                    "eid": e["id"],
                    "tid": tid,
                },
            )
            expense_ids.append(str(e["id"]))

        # Soft-delete de la marca-proveedor.
        await session.execute(
            text(
                "UPDATE suppliers SET deactivated_at = now() "
                "WHERE id = :sid AND tenant_id = :tid"
            ),
            {"sid": sid, "tid": tid},
        )

        decision_data = {
            "supplier_id": str(sid),
            "supplier_name": name,
            "collapsed_to_sentinel": str(sentinel_id),
            "expense_ids": expense_ids,
            "product_ids": product_ids,
            "movements_attributed": movements_attributed,
            "before": {"deactivated_at": None},
            "after": {"deactivated_at": "now()"},
        }
        await session.execute(
            text(
                "INSERT INTO decision_audit_log "
                "(id, tenant_id, decision_type, decision_data, triggered_by, created_at) "
                "VALUES (gen_random_uuid(), :tid, :dt, CAST(:dd AS jsonb), :tb, now())"
            ),
            {
                "tid": tid,
                "dt": _DECISION_TYPE_MERCH,
                "dd": json.dumps(decision_data),
                "tb": "script:deactivate_brand_suppliers",
            },
        )
        applied += 1
    return applied


def _print_summary(tid: uuid.UUID, candidates: list[dict[str, Any]]) -> None:
    from collections import Counter

    by_reason = Counter(c["reason"] for c in candidates)
    print(f"tenant {tid}: {len(candidates)} candidato(s) — {dict(by_reason)}")
    for c in candidates:
        if c["reason"] in (_BRAND_FROM_CATALOG, _MERCH_SOURCE):
            print(f"    [{c['reason']}] {c['name']!r}  supplier={c['supplier_id']}")
    review = [c for c in candidates if c["reason"] == _REVIEW_MANUAL]
    if review:
        print(f"    ({len(review)} para REVISIÓN MANUAL, no se desactivan automático)")


def _write_report(path: str, rows: list[dict[str, Any]]) -> None:
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["tenant_id", "supplier_id", "name", "reason", "matches_brand"]
            )
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
    print(f"\nReporte escrito en {path} ({len(rows)} fila(s)).")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument(
        "--reassign-to-sentinel",
        action="store_true",
        help="Reasignar los productos de las marcas al sentinela 'No identificado'",
    )
    parser.add_argument("--out", help="Path del reporte (CSV si .csv, si no JSON)")
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
        print(f"[{mode}] limpieza de marcas→proveedor en {len(tids)} tenant(s)\n")

        all_rows: list[dict[str, Any]] = []
        total_applied = 0
        for tid in tids:
            candidates = await _classify_tenant(session, tid)
            all_rows.extend(candidates)
            _print_summary(tid, candidates)
            if args.apply:
                applied = await _apply_tenant(
                    session, tid, candidates,
                    reassign_to_sentinel=args.reassign_to_sentinel,
                )
                applied += await _apply_merch_source(session, tid, candidates)
                total_applied += applied
            print()

        if args.out and all_rows:
            _write_report(args.out, all_rows)

        if args.apply:
            await session.commit()
            print(f"COMMIT: {total_applied} proveedor(es) desactivado(s) "
                  f"(decision_type={_DECISION_TYPE} / {_DECISION_TYPE_MERCH}). "
                  f"Ver 'CÓMO REVERTIR' en el docstring.")
        else:
            await session.rollback()
            brand = sum(1 for r in all_rows if r["reason"] == _BRAND_FROM_CATALOG)
            merch = sum(1 for r in all_rows if r["reason"] == _MERCH_SOURCE)
            print(f"Dry-run: nada se escribió. Con --apply se desactivarían: "
                  f"{brand} {_BRAND_FROM_CATALOG} + {merch} {_MERCH_SOURCE} "
                  f"(colapsados a '{_SENTINEL_NAME}').")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
