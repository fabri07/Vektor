"""Bloque 5 — huellas de esquema: insensibles al orden, sensibles al set."""

from __future__ import annotations

from app.domain.ingestion_schema_fingerprint import (
    compute_context_signature,
    compute_schema_fingerprint,
)


def test_columnas_reordenadas_dan_la_misma_firma() -> None:
    ctx_a = {"entity_type": "product", "headers": ["nombre", "precio", "tienda"]}
    ctx_b = {"entity_type": "product", "headers": ["tienda", "nombre", "precio"]}
    assert compute_context_signature(ctx_a) == compute_context_signature(ctx_b)


def test_columna_agregada_cambia_la_firma() -> None:
    ctx_a = {"entity_type": "product", "headers": ["nombre", "precio"]}
    ctx_b = {"entity_type": "product", "headers": ["nombre", "precio", "stock"]}
    assert compute_context_signature(ctx_a) != compute_context_signature(ctx_b)


def test_columna_eliminada_cambia_la_firma() -> None:
    ctx_a = {"entity_type": "product", "headers": ["nombre", "precio", "stock"]}
    ctx_b = {"entity_type": "product", "headers": ["nombre", "precio"]}
    assert compute_context_signature(ctx_a) != compute_context_signature(ctx_b)


def test_misma_columnas_distinta_entidad_da_firma_distinta() -> None:
    ctx_a = {"entity_type": "product", "headers": ["nombre", "monto"]}
    ctx_b = {"entity_type": "expense", "headers": ["nombre", "monto"]}
    assert compute_context_signature(ctx_a) != compute_context_signature(ctx_b)


def test_tildes_y_mayusculas_no_cambian_la_firma() -> None:
    ctx_a = {"entity_type": "product", "headers": ["Precio de Compra", "Tienda"]}
    ctx_b = {"entity_type": "product", "headers": ["precio_de_compra", "tienda"]}
    assert compute_context_signature(ctx_a) == compute_context_signature(ctx_b)


def test_schema_fingerprint_ignora_orden_de_hojas() -> None:
    contexts_a = [
        {"headers": ["nombre", "precio"]},
        {"headers": ["fecha", "monto"]},
    ]
    contexts_b = [
        {"headers": ["fecha", "monto"]},
        {"headers": ["nombre", "precio"]},
    ]
    assert compute_schema_fingerprint("spreadsheet", contexts_a) == compute_schema_fingerprint(
        "spreadsheet", contexts_b
    )


def test_schema_fingerprint_depende_del_tipo_de_archivo() -> None:
    contexts = [{"headers": ["nombre", "precio"]}]
    assert compute_schema_fingerprint("spreadsheet", contexts) != compute_schema_fingerprint(
        "text_document", contexts
    )


def test_schema_fingerprint_no_depende_del_file_id() -> None:
    """Ninguna de las dos funciones recibe un identificador de archivo como
    parámetro — no hay forma de que dos archivos con la misma forma den
    huellas distintas por identidad de archivo."""
    import inspect

    params_fp = set(inspect.signature(compute_schema_fingerprint).parameters)
    params_sig = set(inspect.signature(compute_context_signature).parameters)
    identity_param_names = {"file_id", "upload_id", "uploaded_file_id"}
    assert not (params_fp & identity_param_names)
    assert not (params_sig & identity_param_names)
