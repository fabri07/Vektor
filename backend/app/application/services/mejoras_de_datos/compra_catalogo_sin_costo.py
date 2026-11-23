"""Mejora ``compra_catalogo_sin_costo``: el stock de catálogo cargado como COMPRA que
quedó sin costo y sin gasto.

El importador de catálogo nunca toma una columna «Costo total» como costo unitario y,
sin costo, no crea el gasto de mercadería: una hoja «Producto / Cantidad / Costo total»
con el stock marcado como «La compré ahora» dejó el movimiento ``purchase`` sin costo y
sin ``ExpenseEntry``. El total NO está en la base: sólo en el xlsx original (S3). Esta
mejora lo relee y completa la cadena entera:

    movimiento (costo «estimado») → costo vigente del producto → ventas que pasan a
    costearse → gasto COGS por el TOTAL del archivo → (banco/caja, margen, score)

Clase ``cambia_valores``: nunca corre sola, vista previa + PIN. Escribe plata (un gasto
nuevo) y, si el medio es efectivo, el día puede tener arqueo: el cierre guardado NO se
reescribe; la vista previa lo avisa y la auditoría deja que el dueño lo autorizó.

**Etapa 1 (``detectar``, SQL puro, la usa el panel):** movimiento vivo ``purchase`` /
``catalog_initial_stock`` sin costo, con archivo y fila de origen, SIN gasto vivo de esa
fila, cuyo confirm más reciente dijo «compra» y mapeó una columna «costo total».

**Etapa 2 (``evidencia``, lee S3, sólo vista previa y aplicar, ANTES del lock):** se
re-descarga el archivo, se ubica la fila recomputando el ``source_row_ref`` con el MISMO
ancla del importador y se verifica nombre y cantidad (no se confía sólo en el hash). Lo
que no calza va «a mano» con su motivo; nunca se carga a ciegas.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as imp
from app.application.services.cash_service import normalize_payment_method
from app.application.services.costo_vigente_service import recalcular_costo_vigente
from app.application.services.gasto_de_stock_de_catalogo import (
    MEDIO_DE_PAGO_POR_DEFECTO,
    armar_gasto_de_stock_de_catalogo,
)
from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.application.services.mejoras_de_datos.compra_sin_costo import (
    MAX_A_MANO,
    MAX_REF,
    _columnas_del_confirm,
    _es_costo_total,
)
from app.application.services.mejoras_de_datos.contrato import (
    Candidato,
    Clase,
    Deteccion,
    Mejora,
    MotivoANoCargar,
    Reversion,
)
from app.application.services.mejoras_de_datos.costeo_compartido import (
    costear_ventas_del_movimiento,
    foto_movimiento,
    foto_producto,
    restaurar_costo_de_productos,
    restaurar_ventas_de_la_corrida,
)
from app.application.services.reread_service import _fresh_summary
from app.domain.contrato_de_costo import contrato_declarado
from app.domain.sale_unit import a_unidades_base
from app.persistence.costeo_de_ventas import _dia_de_negocio, ventas_sin_costo_desde
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.cash_close import CashClose
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.transaction import ExpenseEntry

#: Destino del confirm que dice «esta columna es el costo de compra de la línea».
TARGET_COSTO: Final = "purchase_base_cost"
#: Medios de pago que NO se adivinan: ``other`` no dice nada y ``account`` (cuenta
#: corriente) exigiría un proveedor que acá no hay.
_MEDIOS_NO_INTERPRETABLES: Final = frozenset({"other", "account"})
#: Tope de archivos que lee una vista previa (cada uno es una descarga + un parseo).
MAX_ARCHIVOS: Final = 20


@dataclass(frozen=True)
class Evidencia:
    """Lo que el archivo original dice de UNA fila."""

    total: Decimal
    unit_cost: Decimal
    payment_method: str
    medio_por_defecto: bool
    hoja: str | None


def _trata_como_compra(detail: dict[str, Any] | None, cid: str | None = None) -> bool:
    """¿El confirm marcó este stock como «La compré ahora»? ``stock_treatment`` es un
    texto (todas las hojas) o ``{hoja: tratamiento}``. Con ``cid``, mira esa hoja."""
    raw = (detail or {}).get("stock_treatment")
    if isinstance(raw, dict):
        if cid is not None:
            return bool(raw.get(cid) == imp.STOCK_TREATMENT_PURCHASE)
        return any(v == imp.STOCK_TREATMENT_PURCHASE for v in raw.values())
    return bool(raw == imp.STOCK_TREATMENT_PURCHASE)


def _declara_total_de_catalogo(detail: dict[str, Any] | None) -> bool:
    """¿Una columna «costo total» quedó mapeada al costo de compra del producto?"""
    return any(
        destino == TARGET_COSTO and _es_costo_total(col)
        for col, destino in _columnas_del_confirm(detail)
    )


async def _ultimo_confirm_por_archivo(
    session: AsyncSession, tenant_id: uuid.UUID, archivos: set[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any] | None]:
    """El ``detail`` del confirm MÁS RECIENTE de cada archivo (un archivo puede
    haberse confirmado más de una vez; manda el último)."""
    filas = (
        await session.execute(
            select(PipelineEvent.file_id, PipelineEvent.detail)
            .where(
                PipelineEvent.tenant_id == tenant_id,
                PipelineEvent.stage == "confirm",
                PipelineEvent.file_id.in_(archivos),
            )
            .order_by(PipelineEvent.created_at, PipelineEvent.id)
        )
    ).all()
    out: dict[uuid.UUID, dict[str, Any] | None] = {}
    for fid, detail in filas:  # el orden ascendente deja el último
        out[fid] = detail
    return out


def _fecha_del_gasto(mov: InventoryMovement) -> datetime:
    """La fecha del movimiento (el archivo no mapea ninguna otra), SIN zona.

    ``transaction_date`` es un ``timestamp`` sin zona y el ledger guarda la hora
    argentina rotulada UTC: se pasa el mismo valor de pared que usó el importador
    para las dos (``tx_date``). Con zona, asyncpg rechaza el INSERT (SQLite no)."""
    valor = mov.occurred_at or mov.created_at
    if valor is None:
        return datetime.now(UTC).replace(tzinfo=None)
    return valor.astimezone(UTC).replace(tzinfo=None) if valor.tzinfo is not None else valor


class CompraCatalogoSinCosto(Mejora):
    id = "compra_catalogo_sin_costo"
    clase = Clase.CAMBIA_VALORES
    titulo = "Costo y gasto de stock de catálogo cargado como compra"
    necesita_archivo = True

    # ── Etapa 1: SQL puro ────────────────────────────────────────────────────

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
                m.source_type == SOURCE_CATALOG_INITIAL_STOCK,
                m.voided_at.is_(None),
                m.unit_cost.is_(None),
                m.unit_cost_final.is_(None),
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

        validas = []
        for mov, nombre, factor in filas:
            if mov.source_row_ref is None or len(mov.source_row_ref) > MAX_REF:
                a_mano(mov, nombre, "la fila de origen es demasiado larga para cruzarla")
                continue
            validas.append((mov, nombre, factor))
        if not validas:
            return det

        uploads = {mov.source_upload_id for mov, _, _ in validas if mov.source_upload_id}
        refs = {mov.source_row_ref for mov, _, _ in validas}
        con_gasto: set[tuple[Any, Any, Any]] = {
            (g.source_upload_id, g.source_row_ref, g.product_id)
            for g in (
                await session.execute(
                    select(ExpenseEntry).where(
                        ExpenseEntry.tenant_id == tenant_id,
                        ExpenseEntry.source_upload_id.in_(uploads),
                        ExpenseEntry.source_row_ref.in_(refs),
                        ExpenseEntry.voided_at.is_(None),
                    )
                )
            ).scalars()
        }
        confirms = await _ultimo_confirm_por_archivo(session, tenant_id, uploads)

        for mov, nombre, factor in validas:
            clave = (mov.source_upload_id, mov.source_row_ref, mov.product_id)
            detail = confirms.get(mov.source_upload_id) if mov.source_upload_id else None
            if clave in con_gasto:
                a_mano(mov, nombre, "esa fila ya tiene un gasto vivo")
                continue
            if not _trata_como_compra(detail):
                a_mano(mov, nombre, "el archivo no marcó este stock como compra")
                continue
            if not _declara_total_de_catalogo(detail):
                a_mano(mov, nombre, "el archivo no mapeó una columna de costo total")
                continue
            det.candidatos.append(
                Candidato(
                    id=mov.id,
                    etiqueta=nombre,
                    detalle={
                        "producto_id": str(mov.product_id),
                        "archivo_id": str(mov.source_upload_id),
                        "fila": mov.source_row_ref,
                        "qty": mov.qty,
                        "factor": factor,
                        "fecha": _fecha_del_gasto(mov).isoformat(),
                    },
                )
            )
        return det

    # ── Etapa 2: lee S3 ──────────────────────────────────────────────────────

    async def evidencia(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        candidatos: list[Candidato],
        s3: Any,
    ) -> dict[uuid.UUID, Any]:
        por_archivo: dict[uuid.UUID, list[Candidato]] = defaultdict(list)
        for c in candidatos:
            por_archivo[uuid.UUID(str(c.detalle["archivo_id"]))].append(c)

        out: dict[uuid.UUID, Any] = {}
        archivos = list(por_archivo)
        for fid in archivos[MAX_ARCHIVOS:]:
            for c in por_archivo[fid]:
                out[c.id] = MotivoANoCargar("demasiados archivos a la vez: aplicá por tandas")
        archivos = archivos[:MAX_ARCHIVOS]
        confirms = await _ultimo_confirm_por_archivo(session, tenant_id, set(archivos))
        movs = {
            m.id: m
            for m in (
                await session.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.tenant_id == tenant_id,
                        InventoryMovement.id.in_({c.id for c in candidatos}),
                    )
                )
            ).scalars()
        }
        for fid in archivos:
            lista = por_archivo[fid]
            archivo = (
                await session.execute(
                    select(UploadedFile).where(
                        UploadedFile.id == fid, UploadedFile.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if archivo is None or archivo.deleted_at is not None:
                for c in lista:
                    out[c.id] = MotivoANoCargar("el archivo original ya no está")
                continue
            try:
                fresh = await _fresh_summary(archivo, s3)
            except Exception:  # noqa: BLE001 — S3 caído o archivo ilegible: «a mano», no 500
                for c in lista:
                    out[c.id] = MotivoANoCargar("no se pudo leer el archivo original")
                continue
            filas = self._filas_del_archivo(tenant_id, fid, confirms.get(fid), fresh)
            for c in lista:
                out[c.id] = self._evidencia_de(c, movs.get(c.id), filas)
        return out

    @staticmethod
    def _filas_del_archivo(
        tenant_id: uuid.UUID,
        archivo_id: uuid.UUID,
        detail: dict[str, Any] | None,
        fresh: dict[str, Any],
    ) -> dict[str, tuple[str, int, dict[str, Any], dict[str, str]]]:
        """``source_row_ref`` → ``(hoja, índice, fila, columnas)`` de las hojas de compra.

        Recomputa el ref con el MISMO ancla que usó el importador (archivo, hoja,
        índice dentro de la hoja)."""
        out: dict[str, tuple[str, int, dict[str, Any], dict[str, str]]] = {}
        contextos = ((detail or {}).get("mappings") or {}).get("context") or {}
        mapeadas = {str(c.get("context_id")): c for c in fresh.get("mapping_contexts") or []}
        for cid, mapeo in contextos.items():
            ctx = mapeadas.get(str(cid))
            if ctx is None or not _trata_como_compra(detail, str(cid)):
                continue
            cols, _cf, _cruzados, ignoradas = imp._resolve_target_cols(mapeo or {})
            if not (cols.get("name") and cols.get("stock_units") and cols.get(TARGET_COSTO)):
                continue
            if not _es_costo_total(cols[TARGET_COSTO]):
                continue
            bucket = fresh.get(imp._bucket_key_for_context(ctx), [])
            filas = [r for r in bucket if r.get("__context__") == ctx.get("context_id")]
            filas, _motivos = imp.preparar_filas_de_hoja(filas, cols, ignoradas)
            for i, fila in enumerate(filas):
                ref = imp._source_row_ref(imp._import_row_anchor(tenant_id, archivo_id, cid, i))
                if ref is not None:
                    out[ref] = (str(cid), i, fila, cols)
        return out

    @staticmethod
    def _evidencia_de(
        c: Candidato,
        mov: InventoryMovement | None,
        filas: dict[str, tuple[str, int, dict[str, Any], dict[str, str]]],
    ) -> Evidencia | MotivoANoCargar:
        if mov is None:
            return MotivoANoCargar("la compra ya no existe")
        hallada = filas.get(str(c.detalle["fila"]))
        if hallada is None:
            return MotivoANoCargar("no se encontró la fila en el archivo")
        hoja, _i, fila, cols = hallada
        nombre_archivo = imp._normalize_name(str(fila.get(cols["name"]) or ""))
        if nombre_archivo != imp._normalize_name(c.etiqueta):
            return MotivoANoCargar("el producto de esa fila del archivo es otro")
        factor = int(c.detalle.get("factor") or 1)
        cantidad = imp._parse_qty(fila.get(cols["stock_units"]))
        if cantidad <= 0 or a_unidades_base(cantidad, factor) != mov.qty:
            return MotivoANoCargar("la cantidad de esa fila del archivo no coincide")
        total = imp._parse_amount(fila.get(cols[TARGET_COSTO]))
        if total is None or total <= 0:
            return MotivoANoCargar("la fila no trae un costo total válido")
        unitario = imp._costo_unitario_desde_total(total, cantidad)
        if unitario is None:
            return MotivoANoCargar("el costo total no alcanza para costear una unidad")
        crudo = imp._row_val(fila, imp._PAGO_COLS)
        texto = "" if crudo is None else str(crudo).strip()
        if not texto or texto.lower() in {"none", "nan"}:
            medio, por_defecto = MEDIO_DE_PAGO_POR_DEFECTO, True
        else:
            medio, por_defecto = normalize_payment_method(texto), False
            if medio in _MEDIOS_NO_INTERPRETABLES:
                return MotivoANoCargar(f"no se pudo interpretar el medio de pago «{texto}»")
        return Evidencia(
            total=Decimal(total),
            unit_cost=unitario,
            payment_method=medio,
            medio_por_defecto=por_defecto,
            hoja=hoja,
        )

    # ── Vista previa ─────────────────────────────────────────────────────────

    async def vista_previa(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        candidatos: list[Candidato],
        evidencia: Mapping[uuid.UUID, Any],
    ) -> list[dict[str, Any]]:
        movs = {
            m.id: m
            for m in (
                await session.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.tenant_id == tenant_id,
                        InventoryMovement.id.in_({c.id for c in candidatos}),
                    )
                )
            ).scalars()
        }
        cierres = {
            d
            for (d,) in (
                await session.execute(
                    select(CashClose.close_date).where(CashClose.tenant_id == tenant_id)
                )
            ).all()
        }
        filas: list[dict[str, Any]] = []
        for c in candidatos:
            ev = evidencia.get(c.id)
            mov = movs.get(c.id)
            if not isinstance(ev, Evidencia) or mov is None:
                continue
            dia = _dia_de_negocio(mov)
            ventas = await self._ventas_que_se_costean(session, tenant_id, mov)
            filas.append(
                {
                    "id": str(c.id),
                    "etiqueta": c.etiqueta,
                    "qty": mov.qty,
                    "total": str(ev.total),
                    "costo_unitario": str(ev.unit_cost),
                    "fecha": c.detalle["fecha"],
                    "medio_de_pago": ev.payment_method,
                    "medio_por_defecto": ev.medio_por_defecto,
                    "hoja": ev.hoja,
                    "ventas_a_costear": ventas,
                    "arqueo_del_dia": ev.payment_method == "cash" and dia in cierres,
                }
            )
        return filas

    @staticmethod
    async def _ventas_que_se_costean(
        session: AsyncSession, tenant_id: uuid.UUID, mov: InventoryMovement
    ) -> int:
        dia = _dia_de_negocio(mov)
        pid = mov.product_id

        def _sync(s: Any) -> int:
            return len(ventas_sin_costo_desde(s, {(tenant_id, pid): dia}))

        return int(await session.run_sync(_sync))

    # ── Aplicar ──────────────────────────────────────────────────────────────

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
            ev = (evidencia or {}).get(cand.id)
            if not isinstance(ev, Evidencia):
                continue
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
            ya_hay_gasto = (
                await session.execute(
                    select(ExpenseEntry.id).where(
                        ExpenseEntry.tenant_id == tenant_id,
                        ExpenseEntry.source_upload_id == mov.source_upload_id,
                        ExpenseEntry.source_row_ref == mov.source_row_ref,
                        ExpenseEntry.product_id == mov.product_id,
                        ExpenseEntry.voided_at.is_(None),
                    )
                )
            ).first()
            if ya_hay_gasto is not None:
                continue  # alguien cargó el gasto mientras tanto: no se duplica
            producto = (
                await session.execute(
                    select(Product).where(
                        Product.id == mov.product_id, Product.tenant_id == tenant_id
                    )
                )
            ).scalar_one()
            contrato = contrato_declarado(ev.unit_cost, envio="desconocido")
            antes: dict[str, Any] = {
                "movimiento": foto_movimiento(mov),
                "producto": foto_producto(producto),
                "producto_id": str(producto.id),
            }

            mov.unit_cost = ev.unit_cost
            mov.unit_cost_final = contrato.unit_cost_final
            mov.shipping_treatment = mov.shipping_treatment or contrato.shipping_treatment
            mov.tax_treatment = mov.tax_treatment or contrato.tax_treatment
            await session.flush()
            await recalcular_costo_vigente(
                session, tenant_id, [producto.id], movimientos_nuevos={mov.id}
            )
            ventas = await costear_ventas_del_movimiento(session, tenant_id, mov, run)
            ventas_costeadas += ventas

            gasto = armar_gasto_de_stock_de_catalogo(
                tenant_id,
                product_id=producto.id,
                product_name=producto.name,
                amount=ev.total,
                tx_date=_fecha_del_gasto(mov),
                source_upload_id=mov.source_upload_id,
                source_row_ref=mov.source_row_ref,
                payment_method=ev.payment_method,
            )
            session.add(gasto)
            await session.flush()
            con_arqueo = (
                ev.payment_method == "cash"
                and (
                    await session.execute(
                        select(CashClose.id).where(
                            CashClose.tenant_id == tenant_id,
                            CashClose.close_date == _dia_de_negocio(mov),
                        )
                    )
                ).first()
                is not None
            )
            despues: dict[str, Any] = {
                "movimiento": foto_movimiento(mov),
                "producto": foto_producto(producto),
                "producto_id": str(producto.id),
                "ventas_costeadas": ventas,
                "gasto_id": str(gasto.id),
                "total": str(ev.total),
                "medio_de_pago": ev.payment_method,
                # El dueño autorizó crear el gasto con la fecha real aunque ese día
                # ya tenga arqueo: el cierre guardado no se reescribe.
                "autorizo_fecha_con_arqueo": con_arqueo,
            }
            await servicio.registrar_cambio(
                session, tenant_id, run, self, mov.id, antes, despues, None
            )
            n += 1
        run.details_json = {**(run.details_json or {}), "ventas_costeadas": ventas_costeadas}
        run.sales_detected = ventas_costeadas
        return n

    # ── Deshacer ─────────────────────────────────────────────────────────────

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
            gasto = (
                await session.execute(
                    select(ExpenseEntry)
                    .where(
                        ExpenseEntry.id == uuid.UUID(d["despues"]["gasto_id"]),
                        ExpenseEntry.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            # Un gasto ya anulado no bloquea: el efecto buscado (que no cuente) ya está.
            if gasto is not None and gasto.voided_at is None:
                if gasto.has_user_edits or Decimal(gasto.amount) != Decimal(
                    d["despues"]["total"]
                ):
                    res.pendientes[mid] = "el gasto fue editado después de la corrida"
                    pendientes_por_producto.add(pid)
                    continue
                gasto.voided_at = datetime.now(UTC)
                gasto.void_reason = "MANUAL_ADMIN_VOID"
                gasto.voided_by_repair_run_id = run.id
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
