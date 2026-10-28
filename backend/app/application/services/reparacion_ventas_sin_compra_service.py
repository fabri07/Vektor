"""B·5g: las ventas históricas sin compra previa pasan a «Otros» (con ``--apply``).

Desde B·5e ninguna venta nueva entra sin producto o sin una compra o stock
inicial con costo a su fecha. Las que ya estaban cargadas siguen ahí: la
relectura no las vuelve a juzgar a propósito (sería una reparación automática).
Esto es esa reparación, explícita y reversible, que autoriza el dueño:

- **analizar** (sólo lectura): por negocio, las ventas vivas sin producto o sin
  respaldo, por mes y por motivo. No toca nada.
- **reparar(aplicar=True)**: cada una se anula (``void_reason =
  REPAIR_SALE_WITHOUT_PURCHASE``), su descuento de stock se devuelve, y su fila
  queda en «Otros» con el motivo, para resolverla con la compra cargada. Todo en
  una transacción, con una ``DataRepairRun`` que guarda cada par venta↔fila.
- **revertir(run_id)**: restaura las ventas cuya fila de «Otros» sigue PENDING
  (y vuelve a aplicar su descuento y a tomar su identidad de operación); las que
  el usuario ya resolvió o descartó quedan como están, con el motivo. Si un
  import posterior volvió a cargar la venta (el archivo se re-subió con la
  compra), la anulada NO se restaura: serían dos. La identidad es por
  COMPROBANTE —varios renglones comparten una—, así que «re-importada» quiere
  decir que la identidad tiene una venta viva que no es de esta corrida ni un
  renglón hermano que ya estaba vivo al aplicar.

La clasificación es la MISMA que la de la importación (``respaldo_service``:
``es_cobro_de_fiado``, ``motivo_sin_respaldo``, ``catalogo_por_archivo``): una
venta respaldada por el stock del catálogo de su propio archivo, aunque su fecha
siga sin confirmar, no se toca. La fila de «Otros» guarda el cliente de la
venta, igual que la del import, para que resolverla dé la misma venta (con su
deuda si era fiada).

Quedan afuera las ventas del POS: la plata y el turno son reales y no se mueven
desde un script; lo que les falta se ve en su tarea de «Otros» (B·5e-2).
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service, stock_service
from app.application.services.ingestion_import_service import (
    MONTO_CANONICO_KEY,
    ROW_REF_KEY,
    _en_unidad_de_venta,
    filas_internas_de_identidad,
    filas_internas_de_venta,
)
from app.application.services.operation_identity_service import (
    liberar_identidades_de,
    restaurar_vinculos,
    tandas_de_claves,
    vinculos_de,
)
from app.application.services.respaldo_service import (
    MOTIVO_VENTA_SIN_COMPRA,
    MOTIVO_VENTA_SIN_PRODUCTO,
    catalogo_por_archivo,
    es_cobro_de_fiado,
    motivo_sin_respaldo,
    primeros_dias_de_fuente,
)
from app.domain.operation_identity import claves_equivalentes, es_equivalencia_exacta
from app.domain.transaction import PaymentMethod
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.cash_close import CashClose
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.operation_identity import OperationIdentity, OperationIdentityLink
from app.persistence.models.pos_operation import PosOperationLine
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
    unclassified_row_ref,
)

REPAIR_TYPE: Final = "B5G_VENTA_SIN_COMPRA_A_OTROS"
VOID_REASON: Final = "REPAIR_SALE_WITHOUT_PURCHASE"


@dataclass
class Candidata:
    """Valores planos (no el ORM): el análisis se imprime después del rollback."""

    sale_id: uuid.UUID
    monto: Decimal
    dia: date
    motivo: str
    producto: str | None
    product_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    fiada: bool
    con_arqueo: bool
    #: Unidades base por unidad de venta (gramos por kg): la fila de «Otros»
    #: lleva la cantidad en la unidad de VENTA, como la del import.
    factor: int | None = None


@dataclass
class Resumen:
    tenant_id: uuid.UUID
    candidatas: list[Candidata] = field(default_factory=list)
    run_id: uuid.UUID | None = None

    @property
    def monto(self) -> Decimal:
        return sum((c.monto for c in self.candidatas), Decimal("0"))

    @property
    def fiado(self) -> Decimal:
        """Lo que baja la deuda de clientes al anularlas (ventas en cuenta corriente)."""
        return sum((c.monto for c in self.candidatas if c.fiada), Decimal("0"))

    @property
    def en_dias_con_arqueo(self) -> list[Candidata]:
        return [c for c in self.candidatas if c.con_arqueo]

    def por_motivo(self) -> Counter[str]:
        return Counter(
            MOTIVO_VENTA_SIN_PRODUCTO if c.producto is None else MOTIVO_VENTA_SIN_COMPRA
            for c in self.candidatas
        )

    def por_mes(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for c in self.candidatas:
            mes = c.dia.strftime("%Y-%m")
            out[mes] = out.get(mes, Decimal("0")) + c.monto
        return dict(sorted(out.items()))


async def analizar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    desde: date | None = None,
    hasta: date | None = None,
) -> Resumen:
    """Las ventas vivas del negocio sin producto o sin compra previa. SELECT-only.

    Quedan afuera los cobros de fiado (``inflow``, sin distinguir mayúsculas:
    no son ventas) y las ventas del POS (la plata y el turno son reales).
    """
    q = select(SaleEntry).where(
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.voided_at.is_(None),
        ~SaleEntry.id.in_(
            select(PosOperationLine.sale_entry_id).where(
                PosOperationLine.tenant_id == tenant_id,
                PosOperationLine.sale_entry_id.is_not(None),
            )
        ),
    )
    if desde is not None:
        q = q.where(SaleEntry.transaction_date >= datetime.combine(desde, time.min))
    if hasta is not None:
        fin = datetime.combine(hasta + timedelta(days=1), time.min)
        q = q.where(SaleEntry.transaction_date < fin)
    ventas = [
        v
        for v in (await session.execute(q.order_by(SaleEntry.transaction_date))).scalars()
        # La MISMA definición de «cobro de fiado» que la importación.
        if not es_cobro_de_fiado(v.payment_method)
    ]
    pids = {v.product_id for v in ventas if v.product_id is not None}
    primeros = await primeros_dias_de_fuente(session, tenant_id, pids)
    del_catalogo = await catalogo_por_archivo(
        session, tenant_id, [(v.source_upload_id, v.product_id) for v in ventas]
    )
    nombres: dict[uuid.UUID, str] = {}
    factores: dict[uuid.UUID, int] = {}
    if pids:
        for fila in (
            await session.execute(
                select(Product.id, Product.name, Product.base_units_per_sale_unit).where(
                    Product.tenant_id == tenant_id, Product.id.in_(pids)
                )
            )
        ).all():
            nombres[fila[0]] = fila[1]
            factores[fila[0]] = int(fila[2] or 1)
    dias_con_arqueo = set(
        (
            await session.execute(
                select(CashClose.close_date).where(CashClose.tenant_id == tenant_id)
            )
        ).scalars()
    )
    resumen = Resumen(tenant_id=tenant_id)
    for v in ventas:
        dia = v.transaction_date.date()
        nombre = nombres.get(v.product_id, "el producto") if v.product_id else None
        motivo = motivo_sin_respaldo(
            v.product_id,
            dia,
            primeros,
            nombre,
            del_catalogo_del_archivo=(v.source_upload_id, v.product_id) in del_catalogo,
        )
        if motivo is None:
            continue
        resumen.candidatas.append(
            Candidata(
                sale_id=v.id,
                monto=Decimal(v.amount),
                dia=dia,
                motivo=motivo,
                producto=nombre,
                product_id=v.product_id,
                factor=factores.get(v.product_id) if v.product_id else None,
                customer_id=v.customer_id,
                fiada=(v.payment_method or "").lower() == PaymentMethod.ACCOUNT.value,
                con_arqueo=dia in dias_con_arqueo,
            )
        )
    return resumen


async def reparar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    desde: date | None = None,
    hasta: date | None = None,
    incluir_dias_con_arqueo: bool = False,
    creado_por: str = "script:ventas_sin_compra_a_otros",
) -> Resumen:
    """Aplica: anula, devuelve el stock y deja la fila en «Otros». Sin commit.

    Una venta de un día con arqueo cerrado descuadraría ese arqueo (su esperado
    quedó congelado): se excluye salvo ``incluir_dias_con_arqueo``.
    """
    await maintenance_lock_service.acquire_maintenance_lock_exclusive(session, tenant_id)
    resumen = await analizar(session, tenant_id, desde, hasta)
    if not incluir_dias_con_arqueo:
        resumen.candidatas = [c for c in resumen.candidatas if not c.con_arqueo]
    run = DataRepairRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        repair_type=REPAIR_TYPE,
        status="APPLIED",
        dry_run=False,
        candidates_found=len(resumen.candidatas),
        sales_detected=len(resumen.candidatas),
        sales_voided=len(resumen.candidatas),
        details_json={},
    )
    session.add(run)
    await session.flush()
    ahora = datetime.now(UTC)
    pares: list[dict[str, Any]] = []
    # La foto de las identidades ANTES de soltarlas (como la relectura): el
    # revert las vuelve a colgar.
    identidades = await vinculos_de(
        session, tenant_id, [("sale", c.sale_id) for c in resumen.candidatas]
    )
    for c in resumen.candidatas:
        v = await session.get(SaleEntry, c.sale_id)
        assert v is not None
        movimiento = await stock_service._live_sale_movement(v.id, tenant_id, session)
        if movimiento is not None:
            await stock_service.void_movement(movimiento, session, lock_held=True)
        v.voided_at = ahora
        v.void_reason = VOID_REASON
        v.voided_by_repair_run_id = run.id
        suyas = identidades.get(("sale", v.id), [])
        fila: dict[str, Any] = {
            "fecha": c.dia.isoformat(),
            "monto": str(c.monto),
            "cantidad": _en_unidad_de_venta(v.quantity, c.factor),
            "producto": c.producto,
            "medio_de_pago": v.payment_method,
            "detalle": v.notes,
            # Internas (``__``): no se muestran como columnas de la fila.
            "__sale_id__": str(v.id),
            # V-16: el monto lo escribió Véktor («1200.00»), no es una celda.
            MONTO_CANONICO_KEY: "1",
            **filas_internas_de_venta(v),
        }
        if v.source_row_ref:
            fila[ROW_REF_KEY] = v.source_row_ref
        # I6-7: la identidad que se suelta abajo viaja con la fila, para que
        # resolverla la vuelva a tomar. Sólo con archivo: la venta resuelta lleva
        # entonces la referencia de su fila, que es lo que le permite al revert
        # no confundirla con un re-import (ver `revertir`).
        if v.source_upload_id is not None:
            for ident in suyas[:1]:
                fila.update(
                    filas_internas_de_identidad(
                        ident["identity_key"],
                        ident["content_hash"],
                        ident["entity_type"],
                        en_conflicto=False,
                    )
                )
        registro = UnclassifiedRecord(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            source="reanalysis",
            uploaded_file_id=v.source_upload_id,
            context_label=c.motivo[:200],
            row_data={k: val for k, val in fila.items() if val not in (None, "")},
            suggested_entity="sale",
            status=UNCLASSIFIED_STATUS_PENDING,
        )
        session.add(registro)
        pares.append(
            {
                "sale_id": str(v.id),
                "record_id": str(registro.id),
                "movement_id": str(movimiento.id) if movimiento is not None else None,
                "identidades": suyas,
            }
        )
    # La fila vuelve a poder entrar si el usuario la descarta y re-sube el
    # archivo con la compra (si no, quedaría «ya aplicada» para siempre).
    await liberar_identidades_de(
        session, tenant_id, [("sale", c.sale_id) for c in resumen.candidatas]
    )
    # Los renglones hermanos que siguen colgados del mismo comprobante (otra
    # venta del documento, respaldada): el revert no los confunde con un re-import.
    contenido_de = {
        i["identity_key"]: i["content_hash"] for ids in identidades.values() for i in ids
    }
    hermanos = {
        clave: sorted(str(e) for e in ids)
        for clave, ids in (await _ventas_vinculadas(session, tenant_id, contenido_de)).items()
    }
    run.details_json = {
        "repair_type": REPAIR_TYPE,
        "pares": pares,
        "hermanos": hermanos,
        "desde": desde.isoformat() if desde else None,
        "hasta": hasta.isoformat() if hasta else None,
        "monto": str(resumen.monto),
        "fiado": str(resumen.fiado),
        "creado_por": creado_por,
    }
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="REPAIR_SALES_WITHOUT_PURCHASE",
            decision_data={
                "run_id": str(run.id), "ventas": len(pares), "monto": str(resumen.monto)
            },
            triggered_by=creado_por,
            created_at=ahora,
        )
    )
    await session.flush()
    resumen.run_id = run.id
    return resumen


@dataclass
class Reversion:
    restauradas: list[str] = field(default_factory=list)
    resueltas_por_el_usuario: list[str] = field(default_factory=list)
    #: Un import posterior volvió a cargar la venta (tomó su identidad): la
    #: anulada queda anulada y su fila de «Otros» se borra — si no, serían dos.
    reimportadas: list[str] = field(default_factory=list)
    #: Su stock ya se vendió: volver a descontarlo dejaría el stock negativo.
    sin_stock: list[str] = field(default_factory=list)


async def _ventas_vinculadas(
    session: AsyncSession, tenant_id: uuid.UUID, contenido_de: dict[str, str]
) -> dict[str, set[uuid.UUID]]:
    """``{clave: ventas VIVAS colgadas de esa identidad}``.

    ``contenido_de`` es ``{clave del snapshot: su content_hash}``. I6-3/I6-4: un
    import posterior puede haber tomado el MISMO documento con la otra forma de
    clave (la ``k2`` con entidad o con letra, si el snapshot es ``k1``): también
    cuenta, o el revert no vería el re-import y dejaría dos ventas vivas. Las
    del ID externo son el mismo documento con certeza; entre una clave sin letra
    y una con letra decide el contenido (la de la otra letra no es un
    re-import).
    """
    if not contenido_de:
        return {}
    origen: dict[str, str] = {clave: clave for clave in contenido_de}
    for clave in contenido_de:
        for eq in claves_equivalentes(clave, "sale"):
            origen.setdefault(eq, clave)
    # En tandas: con las equivalentes son hasta siete claves por comprobante, y
    # miles de candidatas pasarían el límite de parámetros de asyncpg.
    filas: list[Any] = []
    for tanda in tandas_de_claves(sorted(origen)):
        filas.extend(
            (
                await session.execute(
                    select(
                        OperationIdentity.identity_key,
                        OperationIdentity.content_hash,
                        OperationIdentityLink.entity_id,
                    )
                    .join(
                        OperationIdentityLink,
                        OperationIdentityLink.identity_id == OperationIdentity.id,
                    )
                    .join(SaleEntry, SaleEntry.id == OperationIdentityLink.entity_id)
                    .where(
                        OperationIdentity.tenant_id == tenant_id,
                        OperationIdentity.identity_key.in_(tanda),
                        OperationIdentityLink.entity_type == "sale",
                        SaleEntry.tenant_id == tenant_id,
                        SaleEntry.voided_at.is_(None),
                    )
                )
            ).all()
        )
    out: dict[str, set[uuid.UUID]] = {}
    for clave, contenido, eid in filas:
        propia = origen[clave]
        if (
            clave != propia
            and not es_equivalencia_exacta(clave)
            and contenido != contenido_de[propia]
        ):
            continue
        out.setdefault(propia, set()).add(eid)
    return out


async def revertir(session: AsyncSession, run_id: uuid.UUID) -> Reversion:
    """Restaura las ventas cuya fila de «Otros» sigue PENDING. Sin commit."""
    run = await session.get(DataRepairRun, run_id)
    if run is None or run.repair_type != REPAIR_TYPE or run.tenant_id is None:
        raise ValueError(f"No es una corrida de {REPAIR_TYPE}: {run_id}")
    if run.status != "APPLIED":
        raise ValueError(f"La corrida {run_id} ya está {run.status}.")
    await maintenance_lock_service.acquire_maintenance_lock_exclusive(session, run.tenant_id)
    res = Reversion()
    detalle = run.details_json or {}
    pares_run = detalle.get("pares") or []
    # Los hermanos vivos al aplicar no son un re-import. Se calcula UNA vez,
    # antes de restaurar: lo que el loop vuelve a colgar es de esta corrida (y
    # sus ventas, anuladas, no aparecen entre las vivas).
    conocidas: set[uuid.UUID] = set()
    for ids in (detalle.get("hermanos") or {}).values():
        conocidas.update(uuid.UUID(e) for e in ids)
    # I6-7: la venta que nació de resolver una fila de ESTA corrida toma la
    # identidad del comprobante. No es un re-import: sin esto, resolver un renglón
    # haría que el revert diera por re-importado al hermano todavía pendiente y
    # borrara su fila sin restaurar la venta.
    _refs_de_la_corrida = sorted(
        unclassified_row_ref(uuid.UUID(p["record_id"])) for p in pares_run
    )
    for tanda in tandas_de_claves(_refs_de_la_corrida):
        conocidas.update(
            (
                await session.execute(
                    select(SaleEntry.id).where(
                        SaleEntry.tenant_id == run.tenant_id,
                        SaleEntry.source_row_ref.in_(tanda),
                    )
                )
            )
            .scalars()
            .all()
        )
    contenido_de = {
        i["identity_key"]: i["content_hash"]
        for p in pares_run
        for i in (p.get("identidades") or [])
    }
    reimportadas = {
        clave
        for clave, ids in (
            await _ventas_vinculadas(session, run.tenant_id, contenido_de)
        ).items()
        if ids - conocidas
    }
    for par in pares_run:
        registro = await session.get(UnclassifiedRecord, uuid.UUID(par["record_id"]))
        venta = await session.get(SaleEntry, uuid.UUID(par["sale_id"]))
        if (
            registro is None
            or venta is None
            or registro.status != UNCLASSIFIED_STATUS_PENDING
            or venta.voided_by_repair_run_id != run.id
        ):
            res.resueltas_por_el_usuario.append(par["sale_id"])
            continue
        suyas: list[dict[str, str]] = par.get("identidades") or []
        if any(i["identity_key"] in reimportadas for i in suyas):
            await session.delete(registro)
            res.reimportadas.append(par["sale_id"])
            continue
        mov = (
            await session.get(InventoryMovement, uuid.UUID(par["movement_id"]))
            if par.get("movement_id")
            else None
        )
        if mov is not None and mov.voided_at is not None:
            # Volver a descontar no puede dejar stock negativo (regla del ledger):
            # si se vendió lo que se había devuelto, la venta queda anulada.
            producto = await session.get(Product, mov.product_id)
            if producto is not None and producto.stock_units + mov.qty < 0:
                res.sin_stock.append(par["sale_id"])
                continue
            await stock_service.unvoid_movement(mov, session)
        venta.voided_at = None
        venta.void_reason = None
        venta.voided_by_repair_run_id = None
        if suyas:
            await restaurar_vinculos(
                session,
                run.tenant_id,
                venta.source_upload_id,
                [("sale", venta.id, ident) for ident in suyas],
            )
        await session.delete(registro)
        res.restauradas.append(par["sale_id"])
    run.status = "REVERTED"
    await session.flush()
    return res


__all__ = [
    "REPAIR_TYPE",
    "VOID_REASON",
    "Candidata",
    "Resumen",
    "Reversion",
    "analizar",
    "reparar",
    "revertir",
]
