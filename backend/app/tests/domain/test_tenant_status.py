"""Política pura de estado de cuenta (F1 del panel SUPERADMIN)."""

from __future__ import annotations

import pytest

from app.domain.tenant_status import (
    ESTADOS_ASIGNABLES,
    ESTADOS_OPERATIVOS,
    TENANT_READ_ONLY_CODE,
    TENANT_SUSPENDED_CODE,
    NivelAcceso,
    bloqueo_de_peticion,
    nivel_de_acceso,
    puede_autenticarse,
)

_LOGOUT = ("POST", "/api/v1/auth/logout")
_EXPORT = ("POST", "/api/v1/health-scores/{snapshot_id}/export")


@pytest.mark.parametrize(
    ("estado", "nivel"),
    [
        ("ACTIVE", NivelAcceso.COMPLETO),
        ("TRIAL", NivelAcceso.COMPLETO),
        (" active ", NivelAcceso.COMPLETO),
        ("READ_ONLY", NivelAcceso.SOLO_LECTURA),
        ("SUSPENDED", NivelAcceso.DENEGADO),
        ("", NivelAcceso.DENEGADO),
        (None, NivelAcceso.DENEGADO),
        ("CANCELLED", NivelAcceso.DENEGADO),
    ],
)
def test_nivel_de_acceso_falla_cerrado(estado: str | None, nivel: NivelAcceso) -> None:
    assert nivel_de_acceso(estado) is nivel


def test_los_estados_asignables_son_exactamente_los_cuatro() -> None:
    assert set(ESTADOS_ASIGNABLES) == {"ACTIVE", "TRIAL", "READ_ONLY", "SUSPENDED"}
    assert set(ESTADOS_OPERATIVOS) <= set(ESTADOS_ASIGNABLES)


def test_solo_lectura_autentica_y_suspendida_no() -> None:
    assert puede_autenticarse("READ_ONLY", "OWNER")
    assert not puede_autenticarse("SUSPENDED", "OWNER")
    assert not puede_autenticarse("BANANA", "OWNER")
    assert puede_autenticarse("SUSPENDED", "SUPERADMIN")


def test_solo_lectura_permite_metodos_de_lectura_y_la_lista_cerrada() -> None:
    for metodo in ("GET", "HEAD", "OPTIONS"):
        assert bloqueo_de_peticion("READ_ONLY", "OWNER", metodo, "/x") is None
    assert bloqueo_de_peticion("READ_ONLY", "OWNER", *_EXPORT) is None
    assert bloqueo_de_peticion("READ_ONLY", "OWNER", *_LOGOUT) is None
    for metodo in ("POST", "PATCH", "PUT", "DELETE"):
        assert bloqueo_de_peticion("READ_ONLY", "OWNER", metodo, "/x") == TENANT_READ_ONLY_CODE


def test_suspendida_solo_deja_cerrar_sesion() -> None:
    assert bloqueo_de_peticion("SUSPENDED", "OWNER", *_LOGOUT) is None
    assert bloqueo_de_peticion("SUSPENDED", "OWNER", "GET", "/x") == TENANT_SUSPENDED_CODE
    assert bloqueo_de_peticion("SUSPENDED", "OWNER", *_EXPORT) == TENANT_SUSPENDED_CODE


def test_superadmin_nunca_se_bloquea() -> None:
    for estado in ("SUSPENDED", "READ_ONLY", "BANANA", None):
        assert bloqueo_de_peticion(estado, "SUPERADMIN", "POST", "/x") is None


def test_cuenta_operativa_no_bloquea_nada() -> None:
    assert bloqueo_de_peticion("ACTIVE", "OWNER", "DELETE", "/x") is None
    assert bloqueo_de_peticion("TRIAL", "CASHIER", "POST", "/x") is None
