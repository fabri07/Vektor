"""Hora de negocio de Véktor — fuente única del "día argentino".

Todo lo que agrupe, ventanee o timestampee transacciones de negocio usa esta
zona horaria. El server de producción corre en UTC (Railway): un
`datetime.now()` naive ahí corta el día 3 horas antes y manda las ventas de la
tarde-noche al día siguiente. Por eso:

- `now_ar()` — aware, para lógica que compara con otras horas.
- `now_ar_naive()` — hora AR sin tzinfo, para persistir en columnas DateTime
  naive (`transaction_date`). Es EL default correcto de escritura.
- `today_ar()` — el día de negocio actual.

Argentina no tiene DST desde 2009, pero usamos la IANA por si eso cambia.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def now_ar() -> datetime:
    return datetime.now(AR_TZ)


def now_ar_naive() -> datetime:
    return datetime.now(AR_TZ).replace(tzinfo=None)


def today_ar() -> date:
    return datetime.now(AR_TZ).date()


def fecha_de_negocio(momento: datetime) -> date:
    """El día de negocio (Argentina) de un instante que llega de un cliente.

    Un datetime SIN zona es hora local tal como la tipeó el usuario: su fecha es
    la que dice. Uno CON zona (p. ej. `...T00:30:00Z`) es un instante absoluto, y
    su `.date()` en crudo es el día UTC: a las 21:30 de Argentina ya es mañana,
    y el validador anti-futuro rechazaba una venta hecha hoy. Se lleva a hora
    argentina antes de tomar la fecha.
    """
    if momento.tzinfo is None:
        return momento.date()
    return momento.astimezone(AR_TZ).date()
