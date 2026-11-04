"""Remitente y Reply-To de los mails automáticos (Resend HTTP API)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config.settings import get_settings
from app.integrations.smtp import SMTPClient, remitente


@pytest.mark.parametrize(
    ("direccion", "nombre", "esperado"),
    [
        ("noreply@vek7or.com", "Véktor", "Véktor <noreply@vek7or.com>"),
        ("  noreply@vek7or.com ", "Véktor", "Véktor <noreply@vek7or.com>"),
        # Ya viene con nombre: no se duplica.
        ("Véktor <noreply@vek7or.com>", "Otro", "Véktor <noreply@vek7or.com>"),
        # Sin nombre configurado: la dirección sola.
        ("noreply@vek7or.com", "", "noreply@vek7or.com"),
    ],
)
def test_remitente(direccion: str, nombre: str, esperado: str) -> None:
    assert remitente(direccion, nombre) == esperado


def _enviar(monkeypatch: pytest.MonkeyPatch, **config: str) -> dict[str, Any]:
    """Corre `SMTPClient.send` contra un Resend simulado y devuelve el payload."""
    capturado: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        capturado.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "msg_1"})

    transporte = httpx.MockTransport(handler)
    cliente_real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: cliente_real(transport=transporte, **kw))
    s = get_settings()
    monkeypatch.setattr(s, "RESEND_API_KEY", "re_test")
    for clave, valor in config.items():
        monkeypatch.setattr(s, clave, valor)
    assert SMTPClient().send("cliente@example.com", "Asunto", "<p>hola</p>") == "msg_1"
    return capturado


def test_el_from_lleva_nombre_visible_y_dominio_propio(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _enviar(
        monkeypatch, SMTP_FROM_EMAIL="noreply@vek7or.com", EMAIL_FROM_NAME="Véktor"
    )
    assert payload["from"] == "Véktor <noreply@vek7or.com>"


def test_sin_reply_to_configurado_no_se_manda(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "reply_to" not in _enviar(monkeypatch, EMAIL_REPLY_TO="")


def test_con_reply_to_configurado_se_manda(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _enviar(monkeypatch, EMAIL_REPLY_TO=" soporte@vek7or.com ")
    assert payload["reply_to"] == "soporte@vek7or.com"
