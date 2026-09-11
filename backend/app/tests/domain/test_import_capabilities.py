"""E7a-lite — la comparación de capacidades efectivas.

El recorrido completo (registrar con una compuerta prendida, ejecutar con ella
apagada, nada escrito) vive en
``app/tests/api/v1/test_importacion_asincronica_e2e.py``. Acá van los casos que
ese recorrido no puede montar: sobre todo el juego de compuertas **desparejo**
entre los dos procesos, que es lo que pasa cuando la api y el worker no corren el
mismo código — el estado normal de un deploy de Railway, que los redespliega en
paralelo y sin orden garantizado.
"""

from __future__ import annotations

from app.domain.import_capabilities import (
    capacidades_efectivas,
    diferencias,
    texto_de_diferencias,
)


def test_sin_snapshot_no_hay_diferencia() -> None:
    """``NULL`` es "no hay con qué comparar", no "todo cambió".

    Un intento registrado antes de que la columna existiera tiene que poder
    terminar: hacerlo fallar rompería en el deploy los intentos en vuelo que la
    ruta asíncrona existe para no perder.
    """
    assert diferencias(None, {"A": True}) == {}
    assert diferencias({}, {"A": True}) == {}


def test_una_compuerta_que_se_apago_es_una_diferencia() -> None:
    assert diferencias({"A": True}, {"A": False}) == {"A": (True, False)}


def test_una_compuerta_que_se_prendio_tambien() -> None:
    """Las dos direcciones cambian los números. No hay una "segura"."""
    assert diferencias({"A": False}, {"A": True}) == {"A": (False, True)}


def test_lo_que_no_cambio_no_aparece() -> None:
    assert diferencias({"A": True, "B": False}, {"A": True, "B": False}) == {}


def test_una_compuerta_que_el_ejecutor_no_conoce_es_una_diferencia() -> None:
    """El worker corre código viejo: no tiene la compuerta que el registro midió.

    Ignorarla sería lo peligroso: significa que el ejecutor no tiene la lógica que
    el preview usó, o sí la tiene y no la puede gatear. Las dos cambian lo que
    queda escrito.
    """
    assert diferencias({"A": True, "NUEVA": False}, {"A": True}) == {"NUEVA": (False, None)}


def test_una_compuerta_que_el_registro_no_conocia_tambien() -> None:
    """La dirección inversa: el worker es el nuevo. Mismo riesgo, mismo trato."""
    assert diferencias({"A": True}, {"A": True, "NUEVA": True}) == {"NUEVA": (None, True)}


def test_un_valor_no_booleano_congelado_se_lee_como_booleano() -> None:
    """JSONB devuelve lo que se escribió. Un ``1`` guardado por una versión vieja
    no puede leerse como "distinto de ``True``" y fallar un intento sano."""
    assert diferencias({"A": 1}, {"A": True}) == {}
    assert diferencias({"A": 0}, {"A": False}) == {}


def test_el_texto_nombra_la_compuerta_y_dice_que_hacer() -> None:
    texto = texto_de_diferencias({"PURCHASE_COST_ROLLOUT_TENANT_IDS": (True, False)})
    # El operador necesita el nombre de la variable; el usuario, la acción.
    assert "PURCHASE_COST_ROLLOUT_TENANT_IDS" in texto
    assert "activa → inactiva" in texto
    assert "Volvé a confirmar" in texto
    assert "no se importó nada" in texto


def test_el_texto_distingue_apagada_de_inexistente() -> None:
    """"Inactiva" y "no existía" son diagnósticos distintos: uno es una variable
    de entorno, el otro es una versión de código que no coincide."""
    texto = texto_de_diferencias({"NUEVA": (False, None)})
    assert "no existía" in texto


def test_las_capacidades_efectivas_cubren_las_compuertas_que_cambian_numeros() -> None:
    """Que el snapshot no se quede corto: las cuatro compuertas que cambian qué se
    persiste tienen que estar, y la del registro asíncrono NO —apagarla mientras un
    intento está en vuelo tiene que dejarlo terminar."""
    import uuid

    caps = capacidades_efectivas(uuid.uuid4())
    assert set(caps) == {
        "PURCHASE_COST_ROLLOUT_TENANT_IDS",
        "PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS",
        "CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS",
        "INGESTION_SCHEMA_DECISIONS_ROLLOUT_TENANT_IDS",
    }
    # Sin nadie habilitado (el default), todas en False: el lado seguro.
    assert all(v is False for v in caps.values())
