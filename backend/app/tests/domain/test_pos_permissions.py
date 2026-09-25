"""Reglas puras de permisos de caja (B5)."""

import pytest

from app.domain.pos_permissions import (
    PosPermission,
    effective_pos_permissions,
    normalize_pos_permissions,
)


class TestNormalizar:
    def test_solo_true_booleano_habilita(self) -> None:
        crudo = {"discount": True, "fiado": "true", "void_ticket": 1, "learn_barcode": None}
        assert normalize_pos_permissions(crudo) == {"discount": True}

    def test_claves_desconocidas_se_descartan(self) -> None:
        assert normalize_pos_permissions({"superpoderes": True}) == {}

    @pytest.mark.parametrize("crudo", [None, [], "discount", 42])
    def test_lo_que_no_es_un_objeto_no_habilita_nada(self, crudo: object) -> None:
        assert normalize_pos_permissions(crudo) == {}


class TestEfectivos:
    @pytest.mark.parametrize("rol", ["OWNER", "ADMIN"])
    def test_duenio_y_admin_tienen_todo(self, rol: str) -> None:
        assert effective_pos_permissions(rol, {}) == frozenset(PosPermission)

    @pytest.mark.parametrize("rol", ["VIEWER", "ANALYST"])
    def test_los_demas_roles_no_tienen_nada(self, rol: str) -> None:
        todo = {p.value: True for p in PosPermission}
        assert effective_pos_permissions(rol, todo) == frozenset()

    def test_cajero_solo_lo_habilitado(self) -> None:
        assert effective_pos_permissions("CASHIER", {"fiado": True, "discount": False}) == {
            PosPermission.FIADO
        }
