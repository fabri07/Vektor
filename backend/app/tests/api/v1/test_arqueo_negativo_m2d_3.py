"""M·2d-3: un esperado de efectivo negativo es válido y cierra «caja cuadrada».

Caso del dueño: fondo 100, venta en efectivo 20, retiro del socio en efectivo 50.
El esperado es −30 (el fondo no entra) y en el cajón quedan 70. Contar eso —o
cargar −30 en el modo simple— es caja cuadrada, no un 422 ni un faltante.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from app.domain.business_time import today_ar

_HOY = str(today_ar())


@pytest.fixture(autouse=True)
def _celery(mock_score_trigger: Any) -> None:
    pass


async def _dia_con_retiro(client: AsyncClient, headers: dict[str, Any]) -> None:
    venta = await client.post(
        "/api/v1/sales",
        json={"amount": "20.00", "quantity": 1, "transaction_date": _HOY, "payment_method": "cash"},
        headers=headers,
    )
    assert venta.status_code == 201, venta.text
    retiro = await client.post(
        "/api/v1/capital-movements",
        json={"kind": "retiro", "amount": "50.00", "occurred_on": _HOY, "payment_method": "cash"},
        headers=headers,
    )
    assert retiro.status_code == 201, retiro.text
    preview = (
        await client.get(f"/api/v1/cash-closes/preview?close_date={_HOY}", headers=headers)
    ).json()
    assert preview["expected_total_ars"] == -30.0


async def test_arqueo_completo_con_esperado_negativo_cierra_cuadrado(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    await _dia_con_retiro(client, auth_headers)
    resp = await client.post(
        "/api/v1/cash-closes",
        json={
            "close_date": _HOY,
            # El front ya no lo recorta a 0; el backend lo recalcula igual.
            "counted_total_ars": "-30.00",
            "opening_float_ars": "100.00",
            "cash_denominations": {"20": 3, "10": 1},  # 70 en el cajón
            "voucher_expenses_ars": "0",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["counted_total_ars"] == -30.0
    assert data["difference_ars"] == 0.0
    assert data["result_code"] == "BALANCED"


async def test_modo_simple_acepta_el_neto_negativo(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    await _dia_con_retiro(client, auth_headers)
    resp = await client.post(
        "/api/v1/cash-closes",
        json={"close_date": _HOY, "counted_total_ars": "-30.00"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["difference_ars"] == 0.0
    assert resp.json()["result_code"] == "BALANCED"


async def test_modo_simple_negativo_de_mas_es_faltante(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    await _dia_con_retiro(client, auth_headers)
    resp = await client.post(
        "/api/v1/cash-closes",
        json={"close_date": _HOY, "counted_total_ars": "-40.00"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["difference_ars"] == -10.0
    assert resp.json()["result_code"] == "SHORTAGE"
