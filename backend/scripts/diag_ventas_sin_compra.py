"""Read-only: ventas vivas sin producto o sin compra previa, por negocio (B·5g).

El análisis de ``ventas_sin_compra_a_otros.py`` sin modo de escritura: corre la
misma regla (``reparacion_ventas_sin_compra_service.analizar``) en una
transacción que siempre se revierte. SOLO SELECT. Nunca imprime la URL.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/diag_ventas_sin_compra.py
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/diag_ventas_sin_compra.py x@y.com
"""

from __future__ import annotations

import asyncio
import os
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.dirname(_SCRIPTS))

from ventas_sin_compra_a_otros import main  # noqa: E402

if __name__ == "__main__":
    _quien = ["--email", sys.argv[1]] if len(sys.argv) > 1 else ["--all"]
    sys.argv = [sys.argv[0], *_quien]
    asyncio.run(main())
