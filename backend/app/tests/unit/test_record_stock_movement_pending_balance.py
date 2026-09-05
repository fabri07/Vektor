"""El balance DIFERIDO tiene que ser visible para el movimiento siguiente.

`_record_stock_movement` crea el `InventoryBalance` de un producto nuevo y, en el
camino por lote, lo deja PENDIENTE de INSERT (`pending_balances`) en vez de
emitirlo con un savepoint propio. Un balance pendiente no está en la base: si el
segundo movimiento del mismo producto no lo encuentra, encola OTRO pendiente, lo
pisa en el dict y pierde la cantidad del primero.

Hoy el import pasa siempre `balance_index` junto con `pending_balances`, y el
índice sí lo ve — así que este camino no se alcanza desde el importador. El test
existe porque la función tiene que ser correcta con lo que RECIBE: pasarle sólo
`pending_balances` es una firma válida que hasta este fix corrompía el saldo en
silencio, sin error ni traza.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import (
    _BalancePendiente,
    _record_stock_movement,
)


async def test_dos_movimientos_sin_indice_acumulan_en_un_solo_balance(
    db_session: AsyncSession,
) -> None:
    tenant_id = uuid.uuid4()
    product_id = uuid.uuid4()
    pendientes: dict[uuid.UUID, _BalancePendiente] = {}

    # Primer movimiento: no hay balance en la base → se difiere uno nuevo.
    await _record_stock_movement(
        session=db_session,
        tenant_id=tenant_id,
        product_id=product_id,
        qty=10,
        unit_cost=None,
        movement_type="purchase",
        final_qty=10,
        pending_balances=pendientes,
    )
    # Segundo movimiento del MISMO producto, sin `balance_index` donde encontrarlo.
    await _record_stock_movement(
        session=db_session,
        tenant_id=tenant_id,
        product_id=product_id,
        qty=5,
        unit_cost=None,
        movement_type="purchase",
        final_qty=15,
        pending_balances=pendientes,
    )

    assert list(pendientes) == [product_id], "un solo balance pendiente, no dos"
    pendiente = pendientes[product_id]
    # El delta es la suma de los movimientos: es lo que se le suma a un balance
    # ajeno si al flushear resulta que otra transacción ya lo había creado.
    assert pendiente.delta == 15
    # Y el absoluto en memoria acompaña (10 del alta + 5 del segundo movimiento).
    assert pendiente.balance.current_qty == 15
