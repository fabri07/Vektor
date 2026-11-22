"""Registro de mejoras de datos. Agregar una mejora = una línea acá."""

from __future__ import annotations

from app.application.services.mejoras_de_datos.contrato import Mejora

#: id → mejora.
_REGISTRO: dict[str, Mejora] = {}


def registrar(mejora: Mejora) -> None:
    if mejora.id in _REGISTRO:
        raise ValueError(f"Mejora duplicada: {mejora.id}")
    _REGISTRO[mejora.id] = mejora


def obtener(mejora_id: str) -> Mejora | None:
    return _REGISTRO.get(mejora_id)


def todas() -> list[Mejora]:
    return list(_REGISTRO.values())


def _de_fabrica() -> None:
    # Import tardío y al final: el módulo de la mejora importa el servicio, que a su
    # vez importa este registro.
    from app.application.services.mejoras_de_datos.compra_sin_costo import (  # noqa: PLC0415
        CompraSinCosto,
    )

    registrar(CompraSinCosto())


_de_fabrica()
