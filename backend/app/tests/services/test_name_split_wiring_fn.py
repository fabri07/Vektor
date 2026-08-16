"""F-N — la propuesta de split nombre/apellido viaja en el preview del
import masivo (`PreviewItem.name_split_suggestion`), nunca se aplica sola.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.application.services.customer_extraction_service import parse_customer_records
from app.application.services.customer_import_service import (
    build_import_preview as build_customer_preview,
)
from app.application.services.supplier_import_service import (
    build_import_preview as build_supplier_preview,
)
from app.persistence.models.customer import Customer
from app.persistence.models.supplier import Supplier

_VALID_CUIT = "20-12345678-6"


def _row(**kw: Any) -> dict[str, Any]:
    return kw


class TestClientePreviewConPropuesta:
    def test_fila_a_crear_sin_apellido_trae_la_propuesta(self) -> None:
        records = [_row(name="Juan Perez", customer_type="person", dni="30111222")]
        preview = build_customer_preview(records, [])
        item = preview.items[0]
        assert item.status == "create"
        assert item.name_split_suggestion is not None
        assert item.name_split_suggestion.status == "proposed"
        assert item.name_split_suggestion.first_name == "Juan"
        assert item.name_split_suggestion.last_name == "Perez"

    def test_fila_que_ya_trae_apellido_separado_no_tiene_propuesta(self) -> None:
        records = [
            _row(name="Juan", last_name="Perez", customer_type="person", dni="30111222")
        ]
        preview = build_customer_preview(records, [])
        item = preview.items[0]
        assert item.status == "create"
        assert item.name_split_suggestion is None

    def test_empresa_trae_propuesta_not_applicable_no_none(self) -> None:
        """Distinto de "ya separado": acá SÍ corresponde decir por qué no se
        propone (razón social), no simplemente omitir el campo."""
        records = [
            _row(name="García e Hijos S.A.", customer_type="company", cuit=_VALID_CUIT)
        ]
        preview = build_customer_preview(records, [])
        item = preview.items[0]
        assert item.name_split_suggestion is not None
        assert item.name_split_suggestion.status == "not_applicable"

    def test_fila_a_actualizar_no_calcula_propuesta(self) -> None:
        """Un "update" ya tiene su propio last_name en la ficha existente —
        no es este servicio el que decide pisarlo."""
        existing = [
            Customer(
                tenant_id=uuid.uuid4(), name="Juan Perez", dni="30111222"
            )
        ]
        records = [_row(name="Juan Perez", dni="30111222", customer_type="person")]
        preview = build_customer_preview(records, existing)
        item = preview.items[0]
        assert item.status == "update"
        assert item.name_split_suggestion is None

    def test_fila_invalida_no_calcula_propuesta(self) -> None:
        records = [_row(customer_type="person")]  # sin name → invalid
        preview = build_customer_preview(records, [])
        item = preview.items[0]
        assert item.status == "invalid"
        assert item.name_split_suggestion is None


class TestProveedorPreviewConPropuesta:
    def test_sin_coma_nunca_propone_con_heuristica(self) -> None:
        records = [_row(name="Roberto Gomez", cuil="20-12345678-6")]
        preview = build_supplier_preview(records, [])
        item = preview.items[0]
        assert item.status == "create"
        assert item.name_split_suggestion is not None
        assert item.name_split_suggestion.status == "ambiguous"

    def test_con_coma_propone(self) -> None:
        records = [_row(name="Gomez, Roberto", cuil="20-12345678-6")]
        preview = build_supplier_preview(records, [])
        item = preview.items[0]
        assert item.name_split_suggestion is not None
        assert item.name_split_suggestion.status == "proposed"
        assert item.name_split_suggestion.first_name == "Roberto"
        assert item.name_split_suggestion.last_name == "Gomez"

    def test_fila_a_actualizar_no_calcula_propuesta(self) -> None:
        existing = [Supplier(tenant_id=uuid.uuid4(), name="Roberto Gomez", cuil=_VALID_CUIT)]
        records = [_row(name="Roberto Gomez", cuil=_VALID_CUIT)]
        preview = build_supplier_preview(records, existing)
        item = preview.items[0]
        assert item.status == "update"
        assert item.name_split_suggestion is None


class TestExtremoAExtremoConElParserReal:
    """`_infer_doc_and_type` (customer_extraction_service.py) escribe
    `doc_type` en minúscula ("dni"/"cuit") — un test que arma el dict a mano
    con "DNI" en mayúscula no hubiera agarrado un desacople de casing contra
    el valor REAL que produce el parser. Este va por el parser real."""

    def test_csv_con_una_sola_columna_de_nombre_y_dni_propone_con_confianza_baja(
        self,
    ) -> None:
        csv = b"nombre,dni,celular\nJuan Perez,30111222,+54 11 5555-0000\n"
        records, warnings = parse_customer_records(csv, "clientes.csv", "text/csv")
        assert warnings == []
        assert records[0]["doc_type"] == "dni"  # confirma el valor real, no supuesto
        assert not records[0].get("last_name")

        preview = build_customer_preview(records, [])
        item = preview.items[0]
        assert item.status == "create"
        assert item.name_split_suggestion is not None
        assert item.name_split_suggestion.status == "proposed"
        assert item.name_split_suggestion.first_name == "Juan"
        assert item.name_split_suggestion.last_name == "Perez"
        assert "dni" in item.name_split_suggestion.confidence_basis.lower()
