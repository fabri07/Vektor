"""F-H3.b — el registrador del impacto de un import sobre el stock.

Existe por una razón concreta: el importador tiene **dos** caminos de inserción
—`_insert_multisheet_data` para libros de varias hojas y el bloque inline de
`_insert_confirmed_data_impl` para archivos de una sola— y la proyección tiene
que salir igual por los dos. Un archivo de una hoja de ventas es el import más
común que existe; calcular el impacto sólo para los multi-hoja sería una
funcionalidad a medias que además nadie notaría que falta.

La aritmética no vive acá: está en ``domain/inventory_projection`` (pura, sin
sesión). Esto es sólo la parte que necesita la DB — leer el stock previo de un
producto— y el ruteo por efecto de hoja.

**Registrar ANTES de mutar.** El saldo de apertura de la proyección es el stock
PREVIO al archivo. `_apply_purchase_to_stock` hace ``stock_units += qty``, así
que registrar después leería un saldo que ya incluye esa compra y la contaría
dos veces. El orden de pasada (catálogos → compras → ventas, F-H1/F-H2) hace que
el primer registro de cada producto caiga siempre antes de su primera mutación.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.inventory_effect import HISTORICAL_REPLAY
from app.domain.inventory_projection import (
    ImportImpact,
    ProductProjection,
    project_import_impact,
)
from app.domain.inventory_replay_gate import CreditEvent


class ImportProjectionRecorder:
    """Junta lo que un archivo declara sobre el stock, sin aplicarlo."""

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        product_cache: dict[uuid.UUID, Any] | None = None,
        inventory_effect: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._product_cache = product_cache
        self._inventory_effect = inventory_effect or {}
        self._proyecciones: dict[uuid.UUID, ProductProjection] = {}
        #: F-F — las unidades que este archivo hace ENTRAR, con su fecha. Alimentan
        #: el gate de respaldo (`rows_without_stock_backing`), que sin ellas mira un
        #: saldo estático y manda a "Otros" ventas que las compras del propio
        #: archivo respaldan.
        self._creditos: list[CreditEvent] = []

    def effect_for(self, context_id: str | None) -> str | None:
        """Efecto resuelto de la hoja. ``None`` = esta hoja no habla de inventario.

        F-F.4: el default dejó de ser un valor. Hasta F-F.3 la ausencia caía a
        ``informational`` y ese "sin dato" significaba dos cosas a la vez —«el
        caller no mandó el modo» y «esta hoja no mueve unidades»—, así que había
        que elegir un modo seguro para cubrir las dos. Ahora
        ``resolve_inventory_effects`` omite del dict exactamente las hojas sin
        efecto, y la ausencia dice una sola cosa.
        """
        return self._inventory_effect.get(context_id or "")

    def hojas_con_replay(self) -> int:
        """Cuántas hojas van a aplicar la historia.

        F-F: acá al lado vivía ``degradar_a_informational``, que bajaba una hoja
        cuyo replay no se podía validar. Ya no existe esa situación —las compras del
        archivo entran al gate como créditos datados—, así que este contador vuelve
        a ser simplemente lo que el confirm resolvió.
        """
        return sum(1 for efecto in self._inventory_effect.values() if efecto == HISTORICAL_REPLAY)

    def _cuenta_inventario(self, context_id: str | None) -> bool:
        return self.effect_for(context_id) is not None

    async def _producto(self, product_id: uuid.UUID) -> Any:
        """El ``Product``, del cache primero.

        Con ``autoflush=False`` un producto recién creado está pendiente y
        ``session.get`` no lo ve; el cache sí. Se paga a lo sumo un ``get`` por
        producto, no por fila.
        """
        if self._product_cache is not None and product_id in self._product_cache:
            return self._product_cache[product_id]
        from app.persistence.models.product import Product  # noqa: PLC0415

        producto = await self._session.get(Product, product_id)
        if producto is None or producto.tenant_id != self._tenant_id:
            return None
        if self._product_cache is not None:
            self._product_cache[product_id] = producto
        return producto

    def _proyeccion(
        self, product_id: uuid.UUID, nombre: str | None, saldo_previo: int
    ) -> ProductProjection:
        proyeccion = self._proyecciones.get(product_id)
        if proyeccion is None:
            proyeccion = ProductProjection(
                product_id=product_id,
                product_name=(nombre or "").strip()[:299] or "(sin nombre)",
                saldo_previo=saldo_previo,
            )
            self._proyecciones[product_id] = proyeccion
        return proyeccion

    def declarar_catalogo(
        self,
        product_id: uuid.UUID,
        nombre: str | None,
        saldo_previo: int,
        saldo_declarado: int,
    ) -> None:
        """Una hoja de catálogo declara un ABSOLUTO: pisa la apertura, no se suma.

        Llamar ANTES de escribir ``stock_units``, que es lo que cambia el previo.
        """
        if saldo_declarado <= 0:
            return
        self._proyeccion(product_id, nombre, saldo_previo).saldo_declarado = saldo_declarado

    async def registrar_compra(
        self,
        product_id: uuid.UUID | None,
        day: date,
        qty: int,
        context_id: str | None = None,
    ) -> None:
        """Llamar ANTES de ``_apply_purchase_to_stock`` (ver el docstring del módulo)."""
        if product_id is None or qty <= 0:
            return
        producto = await self._producto(product_id)
        if producto is None:
            return
        # El saldo previo se captura SIEMPRE, aunque la hoja no cuente para la
        # proyección: es lo que el gate usa como apertura, y sin él tendría que
        # leer el stock de hoy —que ya incluye esta compra— y contarla dos veces.
        proyeccion = self._proyeccion(product_id, producto.name, int(producto.stock_units))
        # F-F: el crédito del gate tampoco mira el efecto de la hoja. La compra
        # sube stock en cualquier modo (V16: `inventory_effect` gobierna el
        # descuento de las VENTAS, no la suma de las compras), así que un gate que
        # no la viera mandaría a "Otros" ventas que sí están respaldadas.
        self._creditos.append(CreditEvent(product_id=product_id, day=day, qty=qty))
        if not self._cuenta_inventario(context_id):
            return
        proyeccion.agregar_compra(day, qty)

    def creditos(self) -> list[CreditEvent]:
        """Las unidades que el archivo hace entrar, con fecha, para el gate."""
        return list(self._creditos)

    def productos_que_el_archivo_declara(self) -> set[uuid.UUID]:
        """Productos sobre cuyas unidades el archivo dice ALGO.

        F-F.2: o declara su saldo (una hoja de catálogo) o le compra unidades. En
        los dos casos el cero de ese producto deja de ser "no sé cuánto había".
        """
        return {
            pid
            for pid, proyeccion in self._proyecciones.items()
            if proyeccion.saldo_declarado is not None
        } | {c.product_id for c in self._creditos}

    def apertura_de(self, product_id: uuid.UUID) -> int | None:
        """Saldo del que parte el replay de ese producto: lo declarado, o el previo.

        ``None`` = este archivo todavía no tocó el producto, así que su stock de
        hoy ES el previo y el caller lo lee de la DB.
        """
        proyeccion = self._proyecciones.get(product_id)
        return None if proyeccion is None else proyeccion.apertura

    async def registrar_venta(
        self,
        product_id: uuid.UUID | None,
        day: date,
        qty: int,
        context_id: str | None = None,
    ) -> None:
        if product_id is None or qty <= 0 or not self._cuenta_inventario(context_id):
            return
        producto = await self._producto(product_id)
        if producto is None:
            return
        self._proyeccion(product_id, producto.name, int(producto.stock_units)).agregar_venta(
            day, qty
        )

    def impacto(self) -> ImportImpact:
        return project_import_impact(self._proyecciones)

    def volcar_en(self, counts: dict[str, Any]) -> None:
        """Deja el impacto en ``counts``, que es como el import reporta todo.

        Siempre escribe las tres claves, incluso vacías: un import sin impacto y
        un import que no calculó impacto se leen distinto desde afuera, y sin
        esto el aviso no podría distinguirlos.
        """
        impacto = self.impacto()
        counts["impacto_inventario"] = [p.as_dict() for p in impacto.productos]
        counts["stock_proyectado_negativo"] = len(impacto.negativos)
