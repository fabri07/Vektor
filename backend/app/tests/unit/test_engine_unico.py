"""Toda conexión de la aplicación se crea con ``crear_engine``.

La API y cada job de Celery crean su propio engine. La protección contra un
``U+0000`` en un JSONB vive en el serializador de ``crear_engine``: un engine
armado a mano la saltea, y el worker que falló en producción (ASTERIA,
2026-10-05) era justamente uno armado a mano.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2]
_FABRICA = _APP / "persistence" / "db" / "engine_factory.py"


def test_ningun_engine_se_crea_fuera_de_la_fabrica() -> None:
    infractores: list[str] = []
    for archivo in _APP.rglob("*.py"):
        if "tests" in archivo.relative_to(_APP).parts or archivo == _FABRICA:
            continue
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Call):
                funcion = nodo.func
                nombre = (
                    funcion.id
                    if isinstance(funcion, ast.Name)
                    else funcion.attr
                    if isinstance(funcion, ast.Attribute)
                    else None
                )
                if nombre in ("create_async_engine", "create_engine"):
                    infractores.append(f"{archivo.relative_to(_APP)}:{nodo.lineno}")
    assert not infractores, (
        "Estos lugares crean un engine sin `crear_engine` y se saltean el "
        f"serializador que rechaza nulos: {infractores}"
    )


def test_la_fabrica_pone_el_serializador() -> None:
    from app.persistence.db.engine_factory import crear_engine, serializar_json

    engine = crear_engine("sqlite+aiosqlite:///:memory:")
    try:
        assert getattr(engine.dialect, "_json_serializer", None) is serializar_json
    finally:
        engine.sync_engine.dispose()
