"""B9 — `mixed` es un marcador del servidor, nunca un medio de pago declarable.

Las líneas de una operación de caja pagada con varios métodos llevan `mixed`, y
los lectores las reparten por sus tenders. Si un cliente —el chat, un formulario,
una planilla, la propia caja— pudiera escribir `mixed` en una venta suelta, esa
venta no tendría tenders de dónde salir y su plata no caería en ningún medio.
"""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.application.services.cash_service import normalize_payment_method, save_sale
from app.domain.transaction import MIXED_PAYMENT_METHOD, PaymentMethod
from app.schemas.pos import PosTenderRequest
from app.schemas.transaction import PAYMENT_METHOD_PATTERN


def test_no_es_un_payment_method():
    assert MIXED_PAYMENT_METHOD not in {m.value for m in PaymentMethod}
    assert MIXED_PAYMENT_METHOD not in PAYMENT_METHOD_PATTERN


def test_un_tender_no_puede_declararse_mixed():
    with pytest.raises(ValidationError):
        PosTenderRequest(payment_method=MIXED_PAYMENT_METHOD, amount_ars=Decimal("10"))


def test_el_camino_del_chat_no_lo_acepta():
    assert normalize_payment_method(MIXED_PAYMENT_METHOD) == "other"


async def test_save_sale_rechaza_mixed():
    with pytest.raises(ValueError, match="payment_method_required_for_sale"):
        await save_sale(
            {"amount": 100, "payment_method": MIXED_PAYMENT_METHOD},
            uuid.uuid4(),
            uuid.uuid4(),
            None,  # falla antes de tocar la sesión
        )
