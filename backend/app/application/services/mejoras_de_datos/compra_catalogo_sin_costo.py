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

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, NamedTuple, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as imp
from app.application.services.cash_service import normalize_payment_method
from app.application.services.costo_vigente_service import recalcular_costo_vigente
from app.application.services.file_parsing import parse_uploaded_content
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
from app.domain.contrato_de_costo import contrato_declarado
from app.domain.sale_unit import a_unidades_base
from app.persistence.costeo_de_ventas import _dia_de_negocio, contar_ventas_sin_costo
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.cash_close import CashClose
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.pipeline_event import PipelineEvent
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.transaction import ExpenseEntry

#: Destinos del confirm que dicen «esta columna es el costo de compra de la línea»: el
#: de una hoja de un libro multihoja y el de la tabla suelta (camino plano del importador).
TARGETS_COSTO: Final = ("purchase_base_cost", "line_total_cost_ars")
#: Medios de pago que NO se adivinan: ``other`` no dice nada y ``account`` (cuenta
#: corriente) exigiría un proveedor que acá no hay.
_MEDIOS_NO_INTERPRETABLES: Final = frozenset({"other", "account"})
#: Tamaño de lote de los ``IN (...)``: asyncpg no admite más de 32.767 parámetros por
#: consulta, y un catálogo grande los supera. No es un tope: se parte y se sigue.
LOTE_IN: Final = 1000

_T = TypeVar("_T")


def _en_lotes(items: Sequence[_T], tamano: int | None = None) -> Iterator[Sequence[_T]]:
    paso = tamano or LOTE_IN
    for i in range(0, len(items), paso):
        yield items[i : i + paso]


class _Fila(NamedTuple):
    """Una fila ubicada del archivo, con las columnas que se leen de ella."""

    hoja: str | None
    fila: dict[str, Any]
    col_nombre: str
    col_cantidad: str
    col_total: str


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
        destino in TARGETS_COSTO and _es_costo_total(col)
        for col, destino in _columnas_del_confirm(detail)
    )


async def _ultimo_confirm_por_archivo(
    session: AsyncSession, tenant_id: uuid.UUID, archivos: set[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any] | None]:
    """El ``detail`` del confirm MÁS RECIENTE de cada archivo (un archivo puede
    haberse confirmado más de una vez; manda el último)."""
    out: dict[uuid.UUID, dict[str, Any] | None] = {}
    for lote in _en_lotes(sorted(archivos, key=str)):
        filas = (
            await session.execute(
                select(PipelineEvent.file_id, PipelineEvent.detail)
                .where(
                    PipelineEvent.tenant_id == tenant_id,
                    PipelineEvent.stage == "confirm",
                    PipelineEvent.file_id.in_(lote),
                )
                .order_by(PipelineEvent.created_at, PipelineEvent.id)
            )
        ).all()
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


async def _leer_archivo(archivo: UploadedFile, s3: Any) -> dict[str, Any]:
    """Re-descarga el xlsx y lo parsea. El parseo es CPU puro y síncrono: va a un hilo
    para no trabar el event loop (y con él a los demás tenants de la instancia)."""
    contenido = await s3.download(archivo.s3_key)
    return await asyncio.to_thread(
        parse_uploaded_content, contenido, archivo.content_type, archivo.original_filename
    )


class _Cols(NamedTuple):
    por_destino: dict[str, str]
    nombre: str
    cantidad: str
    total: str


def _columnas_de_compra(mapeo: dict[str, str]) -> tuple[_Cols | None, set[str]]:
    """Las columnas de nombre, cantidad y «costo total» de un mapeo, y las ignoradas.
    ``None`` si la hoja no declara las tres (el total, al costo de compra)."""
    cols, _cf, _cruzados, ignoradas = imp._resolve_target_cols(mapeo or {})
    nombre, cantidad = cols.get("name"), cols.get("stock_units")
    total = next(
        (cols[t] for t in TARGETS_COSTO if cols.get(t) and _es_costo_total(cols[t])), None
    )
    if not (nombre and cantidad and total):
        return None, ignoradas
    return _Cols(cols, nombre, cantidad, total), ignoradas


def _indexar(
    out: dict[str, _Fila],
    tenant_id: uuid.UUID,
    archivo_id: uuid.UUID,
    contexto: str | None,
    filas: list[dict[str, Any]],
    cols: _Cols | None,
    *,
    hoja: str | None,
) -> None:
    if cols is None:
        return
    for i, fila in enumerate(filas):
        ref = imp._source_row_ref(imp._import_row_anchor(tenant_id, archivo_id, contexto, i))
        if ref is not None:
            out[ref] = _Fila(hoja, fila, cols.nombre, cols.cantidad, cols.total)


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

        def _consulta(lote: Sequence[uuid.UUID] | None) -> Any:
            c = (
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
            )
            if lote is not None:
                c = c.where(m.id.in_(lote))
            if bloquear:
                c = c.with_for_update(of=m)
            return c

        filas: list[Any] = []
        if ids is None:
            filas.extend((await session.execute(_consulta(None))).all())
        else:
            for lote_ids in _en_lotes(sorted(ids, key=str)):
                filas.extend((await session.execute(_consulta(lote_ids))).all())
        filas.sort(key=lambda f: f[0].id)

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
        refs = sorted({mov.source_row_ref for mov, _, _ in validas if mov.source_row_ref})
        # Por ``source_row_ref`` (un sha256, no se repite) y en lotes; el archivo y el
        # producto se cruzan acá, en la tupla.
        con_gasto: set[tuple[Any, Any, Any]] = set()
        for lote_refs in _en_lotes(refs):
            for g in (
                await session.execute(
                    select(ExpenseEntry).where(
                        ExpenseEntry.tenant_id == tenant_id,
                        ExpenseEntry.source_row_ref.in_(lote_refs),
                        ExpenseEntry.voided_at.is_(None),
                    )
                )
            ).scalars():
                con_gasto.add((g.source_upload_id, g.source_row_ref, g.product_id))
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
        """Lee TODO archivo con candidatos, de a uno: no hay tope de archivos (un archivo
        que no se pudiera leer no puede dejar sin atender a los demás)."""
        por_archivo: dict[uuid.UUID, list[Candidato]] = defaultdict(list)
        for c in candidatos:
            por_archivo[uuid.UUID(str(c.detalle["archivo_id"]))].append(c)

        out: dict[uuid.UUID, Any] = {}
        archivos = list(por_archivo)
        confirms = await _ultimo_confirm_por_archivo(session, tenant_id, set(archivos))
        movs: dict[uuid.UUID, InventoryMovement] = {}
        for lote in _en_lotes(sorted({c.id for c in candidatos}, key=str)):
            movs.update(
                {
                    m.id: m
                    for m in (
                        await session.execute(
                            select(InventoryMovement).where(
                                InventoryMovement.tenant_id == tenant_id,
                                InventoryMovement.id.in_(lote),
                            )
                        )
                    ).scalars()
                }
            )
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
                fresh = await _leer_archivo(archivo, s3)
            except Exception:  # noqa: BLE001 — S3 caído o archivo ilegible: «a mano», no 500
                for c in lista:
                    out[c.id] = MotivoANoCargar("no se pudo leer el archivo original")
                continue
            filas = self._filas_del_archivo(tenant_id, fid, confirms.get(fid), fresh)
            del fresh  # un archivo a la vez: el resumen entero no se retiene
            for c in lista:
                out[c.id] = self._evidencia_de(c, movs.get(c.id), filas)
        return out

    @staticmethod
    def _filas_del_archivo(
        tenant_id: uuid.UUID,
        archivo_id: uuid.UUID,
        detail: dict[str, Any] | None,
        fresh: dict[str, Any],
    ) -> dict[str, _Fila]:
        """``source_row_ref`` → fila ubicada del archivo, con el MISMO ancla que usó el
        importador. Son DOS caminos y el ancla es distinta:

        - **libro de varias hojas** (``mixed``/``multi_sheet``): las filas de cada hoja
          (bucket + ``__context__``), índice dentro de la hoja, ancla con la HOJA;
        - **tabla suelta** (camino plano): TODAS las filas del bucket, índice sobre la
          lista entera, ancla SIN contexto (``None``). El mapeo se guarda igual por hoja.
        """
        mappings = (detail or {}).get("mappings") or {}
        por_hoja: dict[str | None, dict[str, str]] = {
            str(cid): dict(m or {}) for cid, m in (mappings.get("context") or {}).items()
        }
        if not por_hoja and mappings.get("flat"):
            por_hoja = {None: dict(mappings["flat"])}
        multihoja = fresh.get("inferred_type") == "mixed" or bool(fresh.get("multi_sheet"))
        out: dict[str, _Fila] = {}
        if multihoja:
            mapeadas = {str(c.get("context_id")): c for c in fresh.get("mapping_contexts") or []}
            for cid, mapeo in por_hoja.items():
                ctx = mapeadas.get(str(cid))
                if cid is None or ctx is None or not _trata_como_compra(detail, cid):
                    continue
                cols, ignoradas = _columnas_de_compra(mapeo)
                if cols is None:
                    continue
                bucket = fresh.get(imp._bucket_key_for_context(ctx), [])
                filas = [r for r in bucket if r.get("__context__") == ctx.get("context_id")]
                filas, _motivos = imp.preparar_filas_de_hoja(filas, cols.por_destino, ignoradas)
                _indexar(out, tenant_id, archivo_id, cid, filas, cols, hoja=cid)
            return out
        # Tabla suelta: una sola hoja (el importador sólo la trata así con UN contexto).
        if len(por_hoja) != 1 or fresh.get("inferred_type") != "stock":
            return out
        cid, mapeo = next(iter(por_hoja.items()))
        if not _trata_como_compra(detail, cid):
            return out
        cols, ignoradas = _columnas_de_compra(mapeo)
        if cols is None:
            return out
        filas = imp._sin_columnas_ignoradas(list(fresh.get("stock_detectado") or []), ignoradas)
        _indexar(out, tenant_id, archivo_id, None, filas, cols, hoja=cid)
        return out

    @staticmethod
    def _evidencia_de(
        c: Candidato,
        mov: InventoryMovement | None,
        filas: dict[str, _Fila],
    ) -> Evidencia | MotivoANoCargar:
        if mov is None:
            return MotivoANoCargar("la compra ya no existe")
        hallada = filas.get(str(c.detalle["fila"]))
        if hallada is None:
            return MotivoANoCargar("no se encontró la fila en el archivo")
        hoja, fila = hallada.hoja, hallada.fila
        nombre_archivo = imp._normalize_name(str(fila.get(hallada.col_nombre) or ""))
        if nombre_archivo != imp._normalize_name(c.etiqueta):
            return MotivoANoCargar("el producto de esa fila del archivo es otro")
        factor = int(c.detalle.get("factor") or 1)
        cantidad = imp._parse_qty(fila.get(hallada.col_cantidad))
        if cantidad <= 0 or a_unidades_base(cantidad, factor) != mov.qty:
            return MotivoANoCargar("la cantidad de esa fila del archivo no coincide")
        total = imp._parse_amount(fila.get(hallada.col_total))
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
        movs: dict[uuid.UUID, InventoryMovement] = {}
        for lote in _en_lotes(sorted({c.id for c in candidatos}, key=str)):
            movs.update(
                {
                    m.id: m
                    for m in (
                        await session.execute(
                            select(InventoryMovement).where(
                                InventoryMovement.tenant_id == tenant_id,
                                InventoryMovement.id.in_(lote),
                            )
                        )
                    ).scalars()
                }
            )
        cierres = {
            d
            for (d,) in (
                await session.execute(
                    select(CashClose.close_date).where(CashClose.tenant_id == tenant_id)
                )
            ).all()
        }
        validos = [
            (c, evidencia[c.id], movs[c.id])
            for c in candidatos
            if isinstance(evidencia.get(c.id), Evidencia) and c.id in movs
        ]
        # Una consulta por lote de productos, no una por fila.
        fuentes = [(mov.product_id, _dia_de_negocio(mov)) for _, _, mov in validos]

        def _contar(s: Any) -> list[int]:
            return contar_ventas_sin_costo(s, tenant_id, fuentes)

        ventas = await session.run_sync(_contar)
        filas: list[dict[str, Any]] = []
        for (c, ev, mov), n_ventas in zip(validos, ventas, strict=True):
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
                    "ventas_a_costear": n_ventas,
                    "arqueo_del_dia": ev.payment_method == "cash"
                    and _dia_de_negocio(mov) in cierres,
                }
            )
        return filas

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
                # Aparte de ``foto_movimiento`` (compartida con la ruta A: una clave más
                # rompería la comparación de las corridas ya aplicadas).
                "cost_contract_version": mov.cost_contract_version,
            }

            mov.unit_cost = ev.unit_cost
            # El contrato completo, como cualquier compra declarada (así el costo de
            # referencia lo cuenta como «con contrato»); sólo se completan los vacíos.
            for columna, valor in contrato.columnas().items():
                if columna == "unit_cost_final" or getattr(mov, columna) is None:
                    setattr(mov, columna, valor)
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
                "cost_contract_version": mov.cost_contract_version,
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
            # Lo que Aplicar completó con el contrato vuelve a como estaba.
            mov.shipping_treatment = previo.get("shipping_treatment")
            mov.tax_treatment = previo.get("tax_treatment")
            mov.cost_contract_version = d["antes"].get("cost_contract_version")
            res.revertidos.append(mid)
        await session.flush()

        await restaurar_costo_de_productos(
            session, tenant_id, por_producto, pendientes_por_producto
        )
        await restaurar_ventas_de_la_corrida(
            session, tenant_id, run, ya, pendientes_por_producto, res
        )
        return res
