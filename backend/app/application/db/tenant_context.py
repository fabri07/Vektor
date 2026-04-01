"""tenant_context — setea app.current_tenant_id en la conexión PostgreSQL para RLS."""

from sqlalchemy.ext.asyncio import AsyncConnection


async def set_tenant_context(conn: AsyncConnection, tenant_id: str) -> None:
    """
    Establece el parámetro de sesión PostgreSQL usado por las políticas RLS.
    Llamar antes de CADA query que involucre datos de tenant.

    Uso:
        async with get_db() as session:
            await set_tenant_context(await session.connection(), str(tenant_id))
            result = await session.execute(...)
    """
    await conn.execute(
        # Usar execute_sync para el SET de sesión (es un comando, no una query)
        # format_string parametrizado para evitar inyección — tenant_id
        # siempre viene del JWT verificado en deps.py, nunca del cliente.
        __import__("sqlalchemy").text(
            "SET app.current_tenant_id = :tid"
        ),
        {"tid": tenant_id},
    )
