"""E6a-B — el código externo como clave fuerte de maestros, y su contracara.

Las dos mitades del contrato:

* **Identidad**: dos filas con el mismo código son la misma entidad, aunque el
  nombre y el documento difieran. Es la única clave que existe para el maestro de
  un kiosco, donde no hay CUIT ni código de barras.
* **Actualización**: que sean la misma entidad NO dice cuál de las dos versiones
  de un teléfono vale. Con la ficha editada a mano, el import se vuelve ADITIVO
  —completa lo vacío, no pisa lo cargado— y lo reporta.

Separarlas es el punto. Sin la segunda, habilitar la primera significa que cada
re-importación borra las correcciones del usuario en silencio, y encima MÁS
seguido, porque el código hace matchear muchas más filas que el documento.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.customer_import_service import apply_import
from app.persistence.models.customer import Customer
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.customer_repository import CustomerRepository


def _fila(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "Panaderia del Sol",
        "external_code": "CLI-007",
        "email": "",
        "phone": "",
    }
    base.update(over)
    return base


async def _existentes(db: AsyncSession, tenant_id: uuid.UUID) -> list[Customer]:
    return await CustomerRepository(db).list_for_dedup(tenant_id)


async def _importar(
    db: AsyncSession, tenant_id: uuid.UUID, filas: list[dict[str, Any]]
) -> Any:
    """`apply_import` re-resuelve contra la DB actual: no confía en el preview."""
    resultado = await apply_import(CustomerRepository(db), tenant_id, filas)
    await db.commit()
    return resultado


# ── Identidad ────────────────────────────────────────────────────────────────
async def test_el_mismo_codigo_es_el_mismo_cliente_aunque_cambie_el_nombre(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El negocio le cambió el nombre de fantasía al cliente en su sistema. Sigue
    siendo el mismo cliente: el código es lo que lo identifica, no el nombre."""
    await _importar(db_session, sample_tenant.tenant_id, [_fila()])
    resultado = await _importar(
        db_session, sample_tenant.tenant_id, [_fila(name="Panaderia El Sol SRL")]
    )
    assert len(resultado.updated_ids) == 1, "tenía que reconocerlo, no crear otro"
    assert not resultado.created_ids
    clientes = await _existentes(db_session, sample_tenant.tenant_id)
    assert len(clientes) == 1
    assert clientes[0].name == "Panaderia El Sol SRL"


async def test_dos_codigos_distintos_son_dos_clientes_aunque_se_llamen_igual(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos sucursales del mismo nombre son dos fichas. El nombre nunca identifica."""
    await _importar(
        db_session,
        sample_tenant.tenant_id,
        [_fila(external_code="CLI-007"), _fila(external_code="CLI-008")],
    )
    assert len(await _existentes(db_session, sample_tenant.tenant_id)) == 2


async def test_los_ceros_a_la_izquierda_no_fusionan_dos_clientes(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """`007` y `7` son dos códigos. Fusionarlos deja una ficha con los datos de
    otra y no salta en ningún total."""
    await _importar(
        db_session,
        sample_tenant.tenant_id,
        [_fila(external_code="007", name="Uno"), _fila(external_code="7", name="Dos")],
    )
    assert len(await _existentes(db_session, sample_tenant.tenant_id)) == 2


async def test_el_mismo_codigo_desde_dos_sistemas_son_dos_clientes(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El "1024" de un sistema no es el "1024" de otro."""
    await _importar(
        db_session,
        sample_tenant.tenant_id,
        [
            _fila(external_code="1024", external_source="tango", name="Uno"),
            _fila(external_code="1024", external_source="bejerman", name="Dos"),
        ],
    )
    assert len(await _existentes(db_session, sample_tenant.tenant_id)) == 2


async def test_sin_ninguna_clave_fuerte_no_se_fusiona_por_nombre(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin código, documento, email ni teléfono, dos filas con el mismo nombre no
    se fusionan: quedan para revisión. El nombre es señal débil y siempre lo fue —
    agregar el código no puede aflojar eso."""
    resultado = await _importar(
        db_session,
        sample_tenant.tenant_id,
        [{"name": "Panaderia del Sol"}, {"name": "Panaderia del Sol"}],
    )
    assert not resultado.created_ids
    assert resultado.needs_review == 2


# ── Actualización: identidad no es "pisá lo que quieras" ─────────────────────
async def test_una_ficha_editada_a_mano_no_se_pisa_y_se_reporta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El caso que motiva `has_user_edits`.

    El usuario corrige el teléfono en la pantalla; después vuelve a importar el
    padrón, que trae el teléfono viejo. Sin la política, el archivo gana y la
    corrección desaparece sin que nadie se entere.
    """
    await _importar(
        db_session, sample_tenant.tenant_id, [_fila(phone="1122223333")]
    )
    cliente = (await _existentes(db_session, sample_tenant.tenant_id))[0]
    cliente.phone = "1199998888"  # la corrección del usuario
    cliente.has_user_edits = True
    await db_session.commit()

    resultado = await _importar(
        db_session, sample_tenant.tenant_id, [_fila(phone="1122223333")]
    )
    db_session.expunge_all()
    cliente = (await _existentes(db_session, sample_tenant.tenant_id))[0]
    assert cliente.phone == "1199998888", "el import pisó una corrección manual"
    assert resultado.preserved_fields >= 1, (
        "y tiene que reportarlo: una actualización que no ocurrió en silencio es "
        "el mismo defecto con otra cara"
    )


async def test_la_politica_es_aditiva_no_un_bloqueo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Editar la ficha no la congela: el import sigue completando lo que el
    usuario nunca cargó. Si fuera un bloqueo, corregir un teléfono impediría para
    siempre que el padrón trajera el email."""
    await _importar(db_session, sample_tenant.tenant_id, [_fila(phone="1122223333")])
    cliente = (await _existentes(db_session, sample_tenant.tenant_id))[0]
    cliente.phone = "1199998888"
    cliente.has_user_edits = True
    await db_session.commit()

    await _importar(
        db_session,
        sample_tenant.tenant_id,
        [_fila(phone="1122223333", email="hola@panaderia.com")],
    )
    db_session.expunge_all()
    cliente = (await _existentes(db_session, sample_tenant.tenant_id))[0]
    assert cliente.phone == "1199998888", "lo cargado no se pisa"
    assert cliente.email == "hola@panaderia.com", "lo vacío sí se completa"


async def test_sin_ediciones_manuales_el_import_actualiza_como_siempre(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La política no cambia el comportamiento de una ficha que nadie tocó: el
    archivo sigue siendo la fuente y actualiza."""
    await _importar(db_session, sample_tenant.tenant_id, [_fila(phone="1122223333")])
    resultado = await _importar(
        db_session, sample_tenant.tenant_id, [_fila(phone="1144445555")]
    )
    db_session.expunge_all()
    cliente = (await _existentes(db_session, sample_tenant.tenant_id))[0]
    assert cliente.phone == "1144445555"
    assert resultado.preserved_fields == 0
