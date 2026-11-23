"""Mejora ``compra_sin_costo``: la compra importada que quedó sin costo unitario.

Una compra de un archivo (``purchase_import``) puede haber quedado con
``unit_cost`` y ``unit_cost_final`` en NULL aunque el MISMO archivo dejó, por la
misma fila, un gasto con el importe de la línea. Esta mejora completa ese vacío:

    costo por unidad de venta = importe del gasto ÷ (cantidad base ÷ factor)

**Evidencia (estricta; ante la duda va a la lista «a mano», nunca se inventa):**

- movimiento vivo ``purchase`` / ``purchase_import``, contrato versión 1, con
  ``unit_cost`` y ``unit_cost_final`` NULL, ``qty > 0``, archivo y fila de origen
  conocidos (``source_row_ref`` de a lo sumo 64: el gasto lo guarda en 64);
- exactamente UN gasto vivo con el mismo ``(archivo, fila, producto)``, importe
  > 0 y sin ediciones manuales, y ese movimiento es la única compra viva de esa
  fila (dos compras por fila no dicen cuál es de cuál);
- el confirm del archivo declaró el costo TOTAL de la línea: una columna a
  ``line_total_cost_ars``, o un encabezado «costo total» mapeado al monto (``amount``)
  del gasto. Sin eso el importe podría ser un precio unitario;
- la división es EXACTA al centavo (``importe(costo, qty, factor) == gasto``).

**Qué escribe:** ``unit_cost`` y ``unit_cost_final`` (por ``contrato_declarado``:
envío e impuesto quedan ``desconocido``, el costo queda «estimado», nunca
«confirmado») y recalcula el costo vigente del producto.

**Ventas:** las ventas ``sin_costo`` que este movimiento pasa a costear se costean
ACÁ, explícitamente y antes del commit, para capturar su «antes» y «después» en
``DataRepairItem`` (mismo patrón que M·2d-2). El ``before_commit`` ya no las
encuentra. «Deshacer» las devuelve a ``sin_costo`` sólo si siguen como las dejó la
corrida; ``cost_avg_*`` no se restauran: son derivadas y las reescribe la próxima
publicación del promedio.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.costo_vigente_service import recalcular_costo_vigente
from app.application.services.inventory_movement_origin import SOURCE_PURCHASE_IMPORT
from app.application.services.mejoras_de_datos.contrato import (
    Candidato,
    Clase,
    Deteccion,
    Mejora,
    Reversion,
)
from app.application.services.mejoras_de_datos.costeo_compartido import (
    costear_ventas_del_movimiento,
    foto_movimiento,
    foto_producto,
    restaurar_costo_de_productos,
    restaurar_ventas_de_la_corrida,
)
from app.domain.contrato_de_costo import contrato_declarado
from app.domain.sale_unit import importe
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.transaction import ExpenseEntry

#: El gasto guarda ``source_row_ref`` en 64; el movimiento en 100. Una fila más
#: larga no se puede cruzar por igualdad sin riesgo de truncado.
MAX_REF: Final = 64
#: Destino del confirm que dice «esta columna es el costo TOTAL de la línea».
TARGET_COSTO_TOTAL: Final = "line_total_cost_ars"
#: Cuántas filas «a mano» se detallan (el resto se resume): un archivo grande
#: puede traer miles y el panel no es una lista de auditoría.
MAX_A_MANO: Final = 50

_CENTAVO: Final = Decimal("0.01")


def _columnas_del_confirm(detail: dict[str, Any] | None) -> list[tuple[str, str]]:
    """``(encabezado, destino)`` de lo que mapeó el confirm, en las dos formas que lo
    guarda: ``mappings`` (``flat`` de una hoja, ``context`` de un libro multihoja) y el
    ``receipt`` (``columns[]``)."""
    out: list[tuple[str, str]] = []
    if not detail:
        return out
    mappings = detail.get("mappings") or {}
    for col, destino in (mappings.get("flat") or {}).items():
        out.append((str(col), str(destino)))
    for columnas in (mappings.get("context") or {}).values():
        for col, destino in (columnas or {}).items():
            out.append((str(col), str(destino)))
    for c in (detail.get("receipt") or {}).get("columns") or []:
        if isinstance(c, dict) and c.get("target_field"):
            out.append((str(c.get("source_column") or ""), str(c["target_field"])))
    return out


def _es_costo_total(encabezado: str) -> bool:
    """Mismo criterio que ``ingestion_import_service._is_total_cost_col``."""
    norm = encabezado.lower().strip().replace(" ", "_")
    return "costo_total" in norm or "total_costo" in norm


def _declara_costo_total(detail: dict[str, Any] | None) -> bool:
    """¿El confirm dice que el importe de la línea es el costo TOTAL?

    Sí si mapeó algo a ``line_total_cost_ars``, o si una columna cuyo encabezado es
    «costo total» quedó como el monto (``amount``) del gasto: el caso real (hoja
    «Compras mercaderia», «Costo total ARS» → ``amount``). Un «Monto» a secas no
    alcanza: podría ser un precio unitario."""
    for col, destino in _columnas_del_confirm(detail):
        if destino == TARGET_COSTO_TOTAL:
            return True
        if destino == "amount" and _es_costo_total(col):
            return True
    return False


def _costo_exacto(total: Decimal, qty: int, factor: int) -> Decimal | None:
    """Costo por unidad de venta tal que ``importe(costo, qty, factor) == total``.

    ``None`` si no existe uno exacto al centavo (o no es positivo): ahí el número
    sería una aproximación presentada como dato."""
    if qty <= 0 or total <= 0:
        return None
    f = factor if factor > 0 else 1
    costo = (total * Decimal(f) / Decimal(qty)).quantize(_CENTAVO, rounding=ROUND_HALF_UP)
    if costo <= 0 or importe(costo, qty, f) != total:
        return None
    return costo


class CompraSinCosto(Mejora):
    id = "compra_sin_costo"
    clase = Clase.COMPLETA_VACIOS
    titulo = "Costo de compras importadas que quedaron sin costo"

    async def detectar(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        ids: set[uuid.UUID] | None = None,
        *,
        bloquear: bool = False,
    ) -> Deteccion:
        m = InventoryMovement
        consulta = (
            select(m, Product.name, Product.base_units_per_sale_unit)
            .join(Product, Product.id == m.product_id)
            .where(
                m.tenant_id == tenant_id,
                Product.tenant_id == tenant_id,
                m.movement_type == "purchase",
                m.source_type == SOURCE_PURCHASE_IMPORT,
                m.voided_at.is_(None),
                m.unit_cost.is_(None),
                m.unit_cost_final.is_(None),
                m.cost_contract_version == 1,
                m.qty > 0,
                m.source_upload_id.is_not(None),
                m.source_row_ref.is_not(None),
            )
            .order_by(m.id)
        )
        if ids is not None:
            consulta = consulta.where(m.id.in_(ids))
        if bloquear:
            consulta = consulta.with_for_update(of=m)
        filas = (await session.execute(consulta)).all()

        det = Deteccion()

        def a_mano(mov: InventoryMovement, nombre: str, motivo: str) -> None:
            if len(det.a_mano) < MAX_A_MANO:
                det.a_mano[f"{nombre} ({str(mov.id)[:8]})"] = motivo

        # Con la fila de origen más larga que la del gasto no hay cruce seguro.
        validas = []
        for mov, nombre, factor in filas:
            if mov.source_row_ref is None or len(mov.source_row_ref) > MAX_REF:
                a_mano(mov, nombre, "la fila de origen es demasiado larga para cruzarla")
                continue
            validas.append((mov, nombre, factor))
        if not validas:
            return det

        uploads = {mov.source_upload_id for mov, _, _ in validas}
        refs = {mov.source_row_ref for mov, _, _ in validas}
        gastos: dict[tuple[Any, Any, Any], list[ExpenseEntry]] = defaultdict(list)
        for g in (
            await session.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.source_upload_id.in_(uploads),
                    ExpenseEntry.source_row_ref.in_(refs),
                    ExpenseEntry.voided_at.is_(None),
                )
            )
        ).scalars():
            gastos[(g.source_upload_id, g.source_row_ref, g.product_id)].append(g)

        compras: dict[tuple[Any, Any, Any], int] = {
            (up, ref, pid): n
            for up, ref, pid, n in (
                await session.execute(
                    select(m.source_upload_id, m.source_row_ref, m.product_id, func.count())
                    .where(
                        m.tenant_id == tenant_id,
                        m.movement_type == "purchase",
                        m.voided_at.is_(None),
                        m.source_upload_id.in_(uploads),
                    )
                    .group_by(m.source_upload_id, m.source_row_ref, m.product_id)
                )
            ).all()
        }

        confirms = (
            await session.execute(
                select(PipelineEvent.file_id, PipelineEvent.detail).where(
                    PipelineEvent.tenant_id == tenant_id,
                    PipelineEvent.stage == "confirm",
                    PipelineEvent.file_id.in_(uploads),
                )
            )
        ).all()
        con_costo_total = {fid for fid, detail in confirms if _declara_costo_total(detail)}

        for mov, nombre, factor in validas:
            clave = (mov.source_upload_id, mov.source_row_ref, mov.product_id)
            if mov.source_upload_id not in con_costo_total:
                a_mano(mov, nombre, "el archivo no mapeó una columna de costo total de la línea")
                continue
            if compras.get(clave, 0) != 1:
                a_mano(mov, nombre, "hay más de una compra viva para la misma fila del archivo")
                continue
            del_gasto = gastos.get(clave, [])
            if len(del_gasto) != 1:
                a_mano(
                    mov,
                    nombre,
                    "no hay un gasto único para esa fila"
                    if not del_gasto
                    else "hay más de un gasto vivo para la misma fila",
                )
                continue
            gasto = del_gasto[0]
            if gasto.has_user_edits or gasto.amount is None or gasto.amount <= 0:
                a_mano(mov, nombre, "el gasto de esa fila fue editado o no tiene importe")
                continue
            costo = _costo_exacto(Decimal(gasto.amount), mov.qty, factor)
            if costo is None:
                a_mano(mov, nombre, "el importe no se divide exacto por la cantidad")
                continue
            det.candidatos.append(
                Candidato(
                    id=mov.id,
                    etiqueta=nombre,
                    detalle={
                        "unit_cost": str(costo),
                        "importe": str(gasto.amount),
                        "qty": mov.qty,
                    },
                )
            )
        return det

    async def aplicar(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        candidatos: list[Candidato],
        run: DataRepairRun,
        evidencia: Mapping[uuid.UUID, Any] | None = None,
    ) -> int:
        from app.application.services.mejoras_de_datos import servicio  # noqa: PLC0415

        n = 0
        ventas_costeadas = 0
        for cand in candidatos:
            mov = (
                await session.execute(
                    select(InventoryMovement)
                    .where(
                        InventoryMovement.id == cand.id,
                        InventoryMovement.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                mov is None
                or mov.voided_at is not None
                or mov.unit_cost is not None
                or mov.unit_cost_final is not None
            ):
                continue  # cambió desde que se detectó: no se pisa
            producto = (
                await session.execute(
                    select(Product).where(
                        Product.id == mov.product_id, Product.tenant_id == tenant_id
                    )
                )
            ).scalar_one()
            costo = Decimal(str(cand.detalle["unit_cost"]))
            contrato = contrato_declarado(costo, envio="desconocido")
            antes: dict[str, Any] = {
                "movimiento": foto_movimiento(mov),
                "producto": foto_producto(producto),
                "producto_id": str(producto.id),
            }

            mov.unit_cost = costo
            mov.unit_cost_final = contrato.unit_cost_final
            mov.shipping_treatment = mov.shipping_treatment or contrato.shipping_treatment
            mov.tax_treatment = mov.tax_treatment or contrato.tax_treatment
            await session.flush()
            await recalcular_costo_vigente(
                session, tenant_id, [producto.id], movimientos_nuevos={mov.id}
            )

            ventas = await costear_ventas_del_movimiento(session, tenant_id, mov, run)
            ventas_costeadas += ventas
            despues: dict[str, Any] = {
                "movimiento": foto_movimiento(mov),
                "producto": foto_producto(producto),
                "producto_id": str(producto.id),
                "ventas_costeadas": ventas,
            }
            await servicio.registrar_cambio(
                session, tenant_id, run, self, mov.id, antes, despues, None
            )
            n += 1
        run.details_json = {**(run.details_json or {}), "ventas_costeadas": ventas_costeadas}
        run.sales_detected = ventas_costeadas
        return n

    async def revertir(
        self, session: AsyncSession, tenant_id: uuid.UUID, run: DataRepairRun
    ) -> Reversion:
        ya = set((run.details_json or {}).get("revertidos") or [])
        filas = [
            f
            for f in (
                await session.execute(
                    select(DecisionAuditLog)
                    .where(
                        DecisionAuditLog.tenant_id == tenant_id,
                        DecisionAuditLog.decision_type == f"MEJORA_DE_DATOS:{self.id}",
                    )
                    .order_by(DecisionAuditLog.created_at, DecisionAuditLog.id)
                )
            ).scalars()
            if f.decision_data.get("run_id") == str(run.id)
        ]
        res = Reversion()
        pendientes_por_producto: set[str] = set()
        por_producto: dict[str, list[DecisionAuditLog]] = defaultdict(list)

        for fila in filas:
            d = fila.decision_data
            mid = str(d["entidad_id"])
            pid = str(d["despues"].get("producto_id"))
            por_producto[pid].append(fila)
            if mid in ya:
                continue
            mov = (
                await session.execute(
                    select(InventoryMovement)
                    .where(
                        InventoryMovement.id == uuid.UUID(mid),
                        InventoryMovement.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if mov is None:
                res.pendientes[mid] = "la compra ya no existe"
                pendientes_por_producto.add(pid)
                continue
            if mov.voided_at is not None or foto_movimiento(mov) != d["despues"]["movimiento"]:
                res.pendientes[mid] = "la compra cambió después de la corrida"
                pendientes_por_producto.add(pid)
                continue
            previo = d["antes"]["movimiento"]
            mov.unit_cost = None if previo["unit_cost"] is None else Decimal(previo["unit_cost"])
            mov.unit_cost_final = (
                None if previo["unit_cost_final"] is None else Decimal(previo["unit_cost_final"])
            )
            res.revertidos.append(mid)
        await session.flush()

        await restaurar_costo_de_productos(
            session, tenant_id, por_producto, pendientes_por_producto
        )
        await restaurar_ventas_de_la_corrida(
            session, tenant_id, run, ya, pendientes_por_producto, res
        )
        return res
