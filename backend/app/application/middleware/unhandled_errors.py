"""Convierte una excepción no manejada en un 500 que el navegador PUEDE leer.

El handler de `@app.exception_handler(Exception)` no alcanza solo: Starlette lo
cuelga de `ServerErrorMiddleware`, que envuelve a TODOS los `add_middleware`.
Su 500 sale por afuera de `CORSMiddleware` —sin `Access-Control-Allow-Origin`—
y por afuera del middleware que pone `X-Trace-Id`. En un front cross-origin el
navegador lo bloquea y axios lo ve como un error sin respuesta: indistinguible
de un corte de red. La caja mostraba "Sin conexión" ante un 500 del servidor.

Este middleware se registra ADENTRO de CORS: atrapa la excepción, arma el 500
con el mismo handler, y la respuesta vuelve a subir por CORS y por el logger de
requests. El handler global queda como última red para lo que falle afuera
(en los propios middlewares externos).
"""

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ErrorHandler = Callable[[Request, Exception], Awaitable[Response]]


class UnhandledErrorMiddleware:
    def __init__(self, app: ASGIApp, handler: ErrorHandler) -> None:
        self.app = app
        self.handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        respuesta_empezada = False

        async def send_wrapper(message: Message) -> None:
            nonlocal respuesta_empezada
            if message["type"] == "http.response.start":
                respuesta_empezada = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Con la respuesta ya empezada (streaming) no se puede mandar otra:
            # se deja subir y la corta el servidor, como antes.
            if respuesta_empezada:
                raise
            response = await self.handler(Request(scope, receive), exc)
            await response(scope, receive, send)
