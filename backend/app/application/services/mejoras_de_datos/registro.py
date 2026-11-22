"""Registro de mejoras de datos. Agregar una mejora = una línea acá."""

from __future__ import annotations

from app.application.services.mejoras_de_datos.contrato import Mejora

#: id → mejora. Vacío hasta que se porte la primera (``compra_sin_costo``).
_REGISTRO: dict[str, Mejora] = {}


def registrar(mejora: Mejora) -> None:
    if mejora.id in _REGISTRO:
        raise ValueError(f"Mejora duplicada: {mejora.id}")
    _REGISTRO[mejora.id] = mejora


def obtener(mejora_id: str) -> Mejora | None:
    return _REGISTRO.get(mejora_id)


def todas() -> list[Mejora]:
    return list(_REGISTRO.values())
