"""¿Las operaciones que acaba de importar este archivo ya estaban, desde otro?

El problema
-----------
El upload ya bloquea (409) la re-subida del MISMO archivo por ``content_hash``, con
un override explícito para el caso legítimo. Eso cubre exactamente un escenario:
que el usuario vuelva a subir el archivo **byte a byte**. Medido: una planilla
idéntica reexportada desde Excel cambia de hash —el zip guarda timestamps—, así
que ese guard no la ve. Y sin él, importar de nuevo las mismas compras duplica
todo en silencio: verificado contra Postgres real, 3 gastos → 6, $12.000 →
$24.000, stock 10 → 20.

Qué hace este módulo, y qué NO hace
-----------------------------------
**Informa candidatos. No borra, no bloquea, no descarta nada.** Es la regla que
fijó el negocio: sin una clave fuerte —ID estable de la operación en el sistema de
origen, o identidad COMPLETA de comprobante (emisor, tipo, serie/punto de venta y
número)— coincidir en importe y fecha **no alcanza** para afirmar que dos
operaciones son la misma. Un kiosco puede comprar dos veces lo mismo el mismo día
al mismo proveedor, y eso son dos compras.

Por eso la señal (fecha + importe) se usa sólo para MIRAR, nunca para actuar. El
aviso le dice al usuario dónde mirar y de qué archivo viene el parecido; la
decisión —dejarlo o borrar el archivo, que ahora revierte bien— es suya.

Por qué después de importar y no antes
--------------------------------------
Antes del import habría que re-derivar qué filas van a entrar y con qué fecha y
monto, duplicando la lógica de mapeo y lectura del importador: dos
interpretaciones de la misma planilla que pueden divergir, que es el defecto que
todo este programa viene a cerrar. Después, en cambio, se comparan las filas
REALMENTE persistidas contra las que ya existían. El precio es que el aviso llega
con los datos adentro — aceptable porque borrar el archivo los revierte.

Acotado a propósito
-------------------
La comparación se limita al rango de fechas del propio archivo y corta en
``_TOPE_FILAS``. Un tenant con años de historia no puede pagar un barrido
completo por cada import, y un chequeo que degrada el confirm sería peor que el
problema que evita. Si el volumen supera el tope, **se dice** que no se pudo
verificar en vez de afirmar que no hay coincidencias.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

logger = get_logger(__name__)

#: Tope de filas ajenas que se traen para comparar. Por encima, el chequeo se
#: declara no concluyente: es preferible a hacer lento el confirm de un tenant
#: grande, y mucho preferible a afirmar "no hay duplicados" sin haber mirado.
_TOPE_FILAS = 5_000


@dataclass
class SolapamientoDetectado:
    """Cuántas operaciones de este import coinciden con otras ya existentes.

    ``concluyente`` distingue "miré y no hay" de "no pude mirar". Sin esa
    diferencia, un tenant demasiado grande para el chequeo se vería igual que uno
    limpio.
    """

    filas: int = 0
    #: ``{nombre de archivo: cuántas filas de este import se le parecen}``.
    por_archivo: dict[str, int] = field(default_factory=dict)
    concluyente: bool = True

    @property
    def hay(self) -> bool:
        return self.filas > 0


def _clave(fecha: Any, monto: Any) -> tuple[date, Decimal]:
    """Día + importe. La hora no entra: dos cargas del mismo hecho económico
    pueden traer horas distintas (o ninguna) y seguir siendo el mismo día."""
    return (fecha.date() if hasattr(fecha, "date") else fecha, Decimal(str(monto)))


async def detectar_solapamiento(
    session: AsyncSession, tenant_id: uuid.UUID, file_id: uuid.UUID
) -> SolapamientoDetectado:
    """Operaciones de ``file_id`` que coinciden con otras de OTRO archivo vivo."""
    resultado = SolapamientoDetectado()

    propias: dict[tuple[date, Decimal], int] = defaultdict(int)
    fechas: list[date] = []
    # Ventas y gastos comparten el nombre de la columna de fecha; se recorren
    # los dos porque un archivo mixto importa a las dos tablas.
    for modelo in (SaleEntry, ExpenseEntry):
        filas = (
            await session.execute(
                select(modelo.transaction_date, modelo.amount).where(
                    modelo.tenant_id == tenant_id,
                    modelo.source_upload_id == file_id,
                    modelo.voided_at.is_(None),
                )
            )
        ).all()
        for fecha, monto in filas:
            clave = _clave(fecha, monto)
            propias[clave] += 1
            fechas.append(clave[0])

    if not propias:
        return resultado

    # Sólo el rango que este archivo cubre: comparar contra toda la historia del
    # tenant sería un barrido completo por import.
    desde, hasta = min(fechas), max(fechas)
    ajenas: dict[tuple[date, Decimal], set[uuid.UUID]] = defaultdict(set)
    total_ajenas = 0
    for modelo in (SaleEntry, ExpenseEntry):
        columna = modelo.transaction_date
        ajenas_filas = (
            await session.execute(
                select(columna, modelo.amount, modelo.source_upload_id)
                .where(
                    modelo.tenant_id == tenant_id,
                    modelo.source_upload_id.is_not(None),
                    modelo.source_upload_id != file_id,
                    modelo.voided_at.is_(None),
                    columna >= _inicio_del_dia(desde),
                    columna <= _fin_del_dia(hasta),
                )
                .limit(_TOPE_FILAS + 1)
            )
        ).all()
        total_ajenas += len(ajenas_filas)
        for fecha, monto, origen in ajenas_filas:
            if origen is not None:
                ajenas[_clave(fecha, monto)].add(origen)

    if total_ajenas > _TOPE_FILAS:
        resultado.concluyente = False
        logger.info(
            "ingestion.solapamiento.no_concluyente",
            tenant_id=str(tenant_id),
            file_id=str(file_id),
            filas_ajenas=total_ajenas,
        )
        return resultado

    coincidentes: dict[uuid.UUID, int] = defaultdict(int)
    for clave, cuantas in propias.items():
        origenes = ajenas.get(clave)
        if not origenes:
            continue
        resultado.filas += cuantas
        for origen in origenes:
            coincidentes[origen] += cuantas

    if coincidentes:
        resultado.por_archivo = await _nombres_de_archivo(session, tenant_id, coincidentes)
    return resultado


def _inicio_del_dia(dia: date) -> Any:
    from datetime import datetime, time  # noqa: PLC0415

    return datetime.combine(dia, time.min)


def _fin_del_dia(dia: date) -> Any:
    from datetime import datetime, time  # noqa: PLC0415

    return datetime.combine(dia, time.max)


async def _nombres_de_archivo(
    session: AsyncSession, tenant_id: uuid.UUID, conteos: dict[uuid.UUID, int]
) -> dict[str, int]:
    """Nombre de archivo en vez de uuid: el aviso lo lee una persona."""
    from app.persistence.models.file import UploadedFile  # noqa: PLC0415

    filas = (
        await session.execute(
            select(UploadedFile.id, UploadedFile.original_filename).where(
                UploadedFile.tenant_id == tenant_id,
                UploadedFile.id.in_(list(conteos)),
            )
        )
    ).all()
    nombres: dict[uuid.UUID, str] = {fila[0]: fila[1] for fila in filas}
    salida: dict[str, int] = {}
    for fid, cuantas in conteos.items():
        etiqueta = nombres.get(fid) or str(fid)
        salida[etiqueta] = salida.get(etiqueta, 0) + cuantas
    return salida


def texto_del_aviso(solapamiento: SolapamientoDetectado) -> str | None:
    """El aviso, en castellano, o ``None`` si no hay nada que decir.

    Nombra los archivos y NO afirma que sean duplicados: dice que se parecen y
    quién tiene que mirar. Afirmarlo exigiría una clave fuerte que estas filas no
    tienen.
    """
    if not solapamiento.concluyente:
        return (
            "No se pudo verificar si estas operaciones ya estaban cargadas: el "
            "período tiene demasiados movimientos previos. Revisalo a mano si "
            "sospechás que el archivo se importó antes."
        )
    if not solapamiento.hay:
        return None
    origenes = ", ".join(
        f"«{nombre}» ({cuantas})" for nombre, cuantas in sorted(solapamiento.por_archivo.items())
    )
    return (
        f"{solapamiento.filas} operación(es) de este archivo coinciden en fecha e "
        f"importe con otras ya importadas desde {origenes}. Puede ser una carga "
        "repetida del mismo período — o compras legítimamente iguales. No se "
        "descartó ninguna: revisalas y, si el archivo estaba duplicado, borralo."
    )
