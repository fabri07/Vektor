"""F-N — `propose_name_split`/`propose_supplier_name_split`: propuesta, nunca
split automático. Casos reales argentinos que el review del plan citó como
los que la heurística vieja rompía."""

from __future__ import annotations

from app.domain.name_split import propose_name_split, propose_supplier_name_split


class TestClienteConComa:
    def test_apellido_coma_nombre(self) -> None:
        p = propose_name_split("Pérez, Juan", customer_type="person")
        assert p.status == "proposed"
        assert p.last_name == "Pérez"
        assert p.first_name == "Juan"

    def test_particula_antes_de_la_coma_queda_en_el_apellido(self) -> None:
        p = propose_name_split("de la Torre, Juan", customer_type="person")
        assert p.status == "proposed"
        assert p.last_name == "de la Torre"
        assert p.first_name == "Juan"

    def test_coma_gana_incluso_con_evidencia_debil(self) -> None:
        # doc_type=DNI (tipo desconocido) sigue siendo elegible, y la coma no
        # es ambigua — no hace falta la confianza alta de customer_type=person.
        p = propose_name_split("García, Marta", doc_type="DNI")
        assert p.status == "proposed"
        assert p.last_name == "García"
        assert p.first_name == "Marta"

    def test_coma_sin_un_lado_completo_es_ambigua(self) -> None:
        p = propose_name_split("Pérez, ", customer_type="person")
        assert p.status == "ambiguous"


class TestClienteSinComaCasosReview:
    """Los 4 ejemplos concretos que el review citó como rotos por la
    heurística vieja aplicada a ciegas — acá siguen sin auto-aplicarse
    (siempre `status="proposed"`, nunca se escriben solos), pero el split
    propuesto puede no ser perfecto para nombres compuestos: es exactamente
    lo que el usuario tiene que poder revisar y corregir antes de confirmar."""

    def test_juan_carlos_perez_propone_pero_no_es_verdad_absoluta(self) -> None:
        p = propose_name_split("Juan Carlos Pérez", customer_type="person")
        assert p.status == "proposed"
        assert p.first_name == "Juan"
        assert p.last_name == "Carlos Pérez"

    def test_maria_de_los_angeles_deja_la_particula_pegada_al_apellido(self) -> None:
        p = propose_name_split("María de los Ángeles", customer_type="person")
        assert p.status == "proposed"
        assert p.first_name == "María"
        # Apellido = todo lo que queda después de la primera palabra,
        # verbatim — la partícula "de" nunca se separa por su cuenta.
        assert p.last_name == "de los Ángeles"

    def test_jose_luis_rodriguez(self) -> None:
        p = propose_name_split("José Luis Rodríguez", customer_type="person")
        assert p.status == "proposed"
        assert p.first_name == "José"
        assert p.last_name == "Luis Rodríguez"

    def test_ana_maria_lopez_garcia(self) -> None:
        p = propose_name_split("Ana María López García", customer_type="person")
        assert p.status == "proposed"
        assert p.first_name == "Ana"
        assert p.last_name == "María López García"


class TestGatePorTipoDeFicha:
    def test_company_nunca_propone_aunque_tenga_coma(self) -> None:
        p = propose_name_split("García e Hijos, S.A.", customer_type="company")
        assert p.status == "not_applicable"
        assert p.first_name is None
        assert p.last_name is None

    def test_person_sin_coma_propone(self) -> None:
        p = propose_name_split("Juan Perez", customer_type="person")
        assert p.status == "proposed"

    def test_tipo_desconocido_con_dni_propone_con_confianza_baja(self) -> None:
        p = propose_name_split("Juan Perez", doc_type="DNI")
        assert p.status == "proposed"
        assert "dni" in p.confidence_basis.lower()

    def test_cuit_solo_no_alcanza_como_evidencia_de_nada(self) -> None:
        p = propose_name_split("Juan Perez", doc_type="CUIT")
        assert p.status == "ambiguous"

    def test_sin_ningun_dato_es_ambigua(self) -> None:
        p = propose_name_split("Juan Perez")
        assert p.status == "ambiguous"

    def test_company_con_dni_contradictorio_sigue_sin_partir(self) -> None:
        """No es un OR plano: la regla vieja (`customer_type == person or
        doc_type == DNI`) hubiera partido esto porque doc_type == DNI. La
        precedencia explícita hace ganar customer_type == company."""
        p = propose_name_split("García e Hijos", customer_type="company", doc_type="DNI")
        assert p.status == "not_applicable"


class TestCasosBorde:
    def test_una_sola_palabra_no_se_puede_partir(self) -> None:
        p = propose_name_split("Madonna", customer_type="person")
        assert p.status == "not_applicable"
        assert p.first_name is None

    def test_nombre_vacio(self) -> None:
        p = propose_name_split("", customer_type="person")
        assert p.status == "not_applicable"

    def test_particula_sola_al_final_es_ambigua(self) -> None:
        p = propose_name_split("Juan de", customer_type="person")
        assert p.status == "ambiguous"

    def test_dos_palabras_de_particula_quedan_enteras_en_el_apellido(self) -> None:
        p = propose_name_split("Juan de la Torre", customer_type="person")
        assert p.status == "proposed"
        assert p.first_name == "Juan"
        assert p.last_name == "de la Torre"


class TestProveedorSinColumnaDeTipo:
    def test_con_coma_propone_igual_que_cliente(self) -> None:
        p = propose_supplier_name_split("Gómez, Roberto")
        assert p.status == "proposed"
        assert p.last_name == "Gómez"
        assert p.first_name == "Roberto"

    def test_sin_coma_nunca_aplica_la_heuristica_de_primera_palabra(self) -> None:
        p = propose_supplier_name_split("Roberto Gómez")
        assert p.status == "ambiguous"
        assert p.first_name is None
        assert p.last_name is None

    def test_ausencia_de_cuit_no_es_evidencia_de_persona(self) -> None:
        """No hay ningún parámetro de doc_type/customer_type porque Supplier
        no tiene esa columna — la función ni siquiera lo acepta."""
        p = propose_supplier_name_split("Distribuidora del Sur")
        assert p.status == "ambiguous"

    def test_una_sola_palabra(self) -> None:
        p = propose_supplier_name_split("Distribuidora")
        assert p.status == "not_applicable"
