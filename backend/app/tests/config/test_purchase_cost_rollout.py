"""F-H6.c/d — la compuerta de rollout por tenant del motor de costos de compra.

Lo que se protege acá no es el parseo de una lista: es que **el default no
habilite a nadie** y que **una variable mal escrita no impida arrancar**. No hay
staging en este repo, así que la variable se estrena directamente en producción;
si un typo tumbara la API, el rollout gradual sería más riesgoso que fusionar
todo de una.
"""

import logging
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from app.config.purchase_cost_rollout import (
    parse_rollout_tenant_ids,
    purchase_cost_enabled_for,
)
from app.config.settings import Settings, get_settings

TENANT_A = "6f2b0c7e-1111-4a2b-8c3d-00000000000a"
TENANT_B = "6f2b0c7e-1111-4a2b-8c3d-00000000000b"
TENANT_FUERA = "6f2b0c7e-1111-4a2b-8c3d-00000000000c"

#: Firma de la fixture ``habilitados``: recibe la lista de tenants habilitados.
Habilitar = Callable[[list[str]], None]


def _settings(**kwargs: Any) -> Settings:
    """Settings de desarrollo sin leer el ``.env`` del entorno donde corren los tests.

    ``ENABLE_EMAIL_VERIFICATION=False`` no tiene nada que ver con el rollout: sin
    ``.env`` queda en su default ``True``, que exige una credencial de Resend que
    acá no hay. Se apaga para que el objeto se construya y el test hable de lo suyo.
    """
    return Settings(
        _env_file=None,
        ENVIRONMENT="development",
        ENABLE_EMAIL_VERIFICATION=False,
        **kwargs,
    )


@pytest.fixture
def habilitados(monkeypatch: pytest.MonkeyPatch) -> Habilitar:
    """Setea la lista habilitada sobre el singleton cacheado de settings.

    ``get_settings()`` es ``lru_cache``: se parchea el objeto, no la función.
    """

    def _set(valores: list[str]) -> None:
        monkeypatch.setattr(get_settings(), "PURCHASE_COST_ROLLOUT_TENANT_IDS", valores)

    return _set


# ── El default ───────────────────────────────────────────────────────────────


def test_el_default_es_la_lista_vacia() -> None:
    """Sin configurar nada, la compuerta no habilita a nadie.

    Es la condición para poder fusionar la rama: desplegada con la lista vacía,
    el motor de costos queda tan apagado como en ``origin/main``.
    """
    assert _settings().PURCHASE_COST_ROLLOUT_TENANT_IDS == []


def test_con_la_lista_vacia_ningun_tenant_esta_habilitado(habilitados: Habilitar) -> None:
    habilitados([])

    assert purchase_cost_enabled_for(uuid.UUID(TENANT_A)) is False
    assert purchase_cost_enabled_for(TENANT_A) is False


# ── A quién habilita ─────────────────────────────────────────────────────────


def test_habilita_solo_a_los_tenants_de_la_lista(habilitados: Habilitar) -> None:
    habilitados([TENANT_A, TENANT_B])

    assert purchase_cost_enabled_for(uuid.UUID(TENANT_A)) is True
    assert purchase_cost_enabled_for(uuid.UUID(TENANT_B)) is True
    assert purchase_cost_enabled_for(uuid.UUID(TENANT_FUERA)) is False


def test_acepta_el_tenant_como_uuid_o_como_string(habilitados: Habilitar) -> None:
    """Las dos puntas se normalizan con la misma función, así que mayúsculas o
    espacios tienen que dar el mismo tenant: si no, la compuerta habilitaría a
    medias según cómo se escribió la variable."""
    habilitados([TENANT_A.upper()])

    assert purchase_cost_enabled_for(uuid.UUID(TENANT_A)) is True
    assert purchase_cost_enabled_for(TENANT_A) is True
    assert purchase_cost_enabled_for(f"  {TENANT_A}  ") is True


def test_un_tenant_que_no_es_uuid_no_queda_habilitado(habilitados: Habilitar) -> None:
    """No habilitar es el lado seguro de la duda."""
    habilitados([TENANT_A])

    assert purchase_cost_enabled_for("no-soy-un-uuid") is False
    assert purchase_cost_enabled_for("") is False


# ── Formatos de la variable ──────────────────────────────────────────────────


def test_csv_con_espacios_habilita_a_los_dos() -> None:
    s = _settings(PURCHASE_COST_ROLLOUT_TENANT_IDS=f"{TENANT_A}, {TENANT_B}")

    assert s.PURCHASE_COST_ROLLOUT_TENANT_IDS == [TENANT_A, TENANT_B]


def test_json_array_tambien_funciona() -> None:
    """El molde de ``CORS_ORIGINS`` acepta las dos formas y no hay motivo para
    que esta variable se escriba distinto — además, en el archivo ``.env`` el
    JSON array es la única forma que pydantic-settings parsea."""
    s = _settings(PURCHASE_COST_ROLLOUT_TENANT_IDS=f'["{TENANT_A}", "{TENANT_B}"]')

    assert s.PURCHASE_COST_ROLLOUT_TENANT_IDS == [TENANT_A, TENANT_B]


def test_una_lista_ya_materializada_pasa_igual() -> None:
    s = _settings(PURCHASE_COST_ROLLOUT_TENANT_IDS=[TENANT_A, TENANT_B])

    assert s.PURCHASE_COST_ROLLOUT_TENANT_IDS == [TENANT_A, TENANT_B]


def test_los_duplicados_son_idempotentes() -> None:
    """La lista es una llave, no un contador."""
    s = _settings(PURCHASE_COST_ROLLOUT_TENANT_IDS=f"{TENANT_A},{TENANT_A},{TENANT_A.upper()}")

    assert s.PURCHASE_COST_ROLLOUT_TENANT_IDS == [TENANT_A]


def test_desde_la_variable_de_entorno_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """La ruta de producción: Railway setea env vars, no un archivo ``.env``.

    Un campo tipado ``list[...]`` hace que pydantic-settings 2.6 intente decodificar
    JSON ANTES de que corra el validador; el csv sobrevive sólo porque
    ``_LenientEnvSource`` devuelve el string crudo cuando eso falla. Este test es
    la compuerta de que ese source siga cubriendo el campo.
    """
    monkeypatch.setenv("PURCHASE_COST_ROLLOUT_TENANT_IDS", f"{TENANT_A},{TENANT_B}")

    assert _settings().PURCHASE_COST_ROLLOUT_TENANT_IDS == [TENANT_A, TENANT_B]


# ── El fail-safe ─────────────────────────────────────────────────────────────


def test_un_uuid_basura_se_descarta_sin_tumbar_el_arranque(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """El caso que justifica todo el módulo.

    La variable se va a setear en producción desde el minuto cero y sin ensayo
    previo. Un typo en un UUID deja a ESE tenant en el comportamiento viejo —que
    es el seguro— y nada más; levantar una excepción dejaría la API sin arrancar
    y a todos los tenants sin servicio por una variable de conveniencia.
    """
    with caplog.at_level(logging.ERROR):
        s = _settings(PURCHASE_COST_ROLLOUT_TENANT_IDS=f"{TENANT_A},pegué-mal-el-uuid,{TENANT_B}")

    # Los válidos siguen funcionando.
    assert s.PURCHASE_COST_ROLLOUT_TENANT_IDS == [TENANT_A, TENANT_B]

    # El error nombra lo que descartó: si no lo nombra, nadie va a encontrar el typo.
    errores = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("pegué-mal-el-uuid" in m for m in errores), errores
    assert any("PURCHASE_COST_ROLLOUT_TENANT_IDS" in m for m in errores), errores


def test_un_json_array_mal_cerrado_tampoco_tumba_el_arranque(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Un corchete sin cerrar cae al camino csv y sus pedazos se descartan."""
    with caplog.at_level(logging.ERROR):
        s = _settings(PURCHASE_COST_ROLLOUT_TENANT_IDS=f'["{TENANT_A}"')

    assert s.PURCHASE_COST_ROLLOUT_TENANT_IDS == []
    assert [r.message for r in caplog.records if r.levelno >= logging.ERROR]


def test_la_lista_entera_basura_deja_la_compuerta_cerrada(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        s = _settings(PURCHASE_COST_ROLLOUT_TENANT_IDS="a, b, c")

    assert s.PURCHASE_COST_ROLLOUT_TENANT_IDS == []


def test_el_parser_nunca_levanta_ante_tipos_raros() -> None:
    """Blindaje del contrato «nunca levanta», no de un caso real de configuración."""
    assert parse_rollout_tenant_ids(None) == []
    assert parse_rollout_tenant_ids("") == []
    assert parse_rollout_tenant_ids("   ") == []
    assert parse_rollout_tenant_ids(",,,") == []
    assert parse_rollout_tenant_ids(12345) == []
    assert parse_rollout_tenant_ids({TENANT_A}) == [TENANT_A]
    assert parse_rollout_tenant_ids([uuid.UUID(TENANT_A)]) == [TENANT_A]
