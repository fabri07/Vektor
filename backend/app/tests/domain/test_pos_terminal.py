"""Credencial de terminal: se guarda el hash, se compara en tiempo constante."""

from app.domain.pos_terminal import generar_secreto, hash_de_secreto, secreto_coincide


def test_secretos_distintos_cada_vez() -> None:
    assert generar_secreto() != generar_secreto()


def test_el_hash_no_contiene_el_secreto() -> None:
    secreto = generar_secreto()
    assert secreto not in hash_de_secreto(secreto)
    assert len(hash_de_secreto(secreto)) == 64


def test_coincide_solo_con_el_propio() -> None:
    secreto = generar_secreto()
    assert secreto_coincide(secreto, hash_de_secreto(secreto))
    assert not secreto_coincide(secreto + "x", hash_de_secreto(secreto))
