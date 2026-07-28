"""Tests de `render_action_email` (Task 7 — extracción de plantilla de emails).

Cubre: escapado de HTML por campo interpolado, omisión del bloque de CTA
cuando no hay `cta_url`, y que el texto plano siempre incluya la URL cuando
está presente.
"""

from app.integrations.email_templates import render_action_email


class TestRenderActionEmailEscaping:
    def test_escapes_eyebrow(self) -> None:
        html, _ = render_action_email(
            eyebrow="<script>alert('x')</script>",
            heading="Heading",
            body="Body",
            cta_label="Ir",
            cta_url="https://example.com",
            footnote=None,
        )
        assert "<script>alert('x')</script>" not in html
        assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html

    def test_escapes_heading(self) -> None:
        html, _ = render_action_email(
            eyebrow="Eyebrow",
            heading="<b>Bold</b> & \"quoted\"",
            body="Body",
            cta_label="Ir",
            cta_url="https://example.com",
            footnote=None,
        )
        assert "<b>Bold</b>" not in html
        assert "&lt;b&gt;Bold&lt;/b&gt;" in html
        assert "&amp;" in html
        assert "&quot;quoted&quot;" in html

    def test_escapes_body(self) -> None:
        html, _ = render_action_email(
            eyebrow="Eyebrow",
            heading="Heading",
            body="<img src=x onerror=alert(1)>",
            cta_label="Ir",
            cta_url="https://example.com",
            footnote=None,
        )
        assert "<img src=x onerror=alert(1)>" not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html

    def test_escapes_cta_label(self) -> None:
        html, _ = render_action_email(
            eyebrow="Eyebrow",
            heading="Heading",
            body="Body",
            cta_label="<b>Click</b>",
            cta_url="https://example.com",
            footnote=None,
        )
        assert "<b>Click</b>" not in html
        assert "&lt;b&gt;Click&lt;/b&gt;" in html

    def test_escapes_cta_url(self) -> None:
        html, _ = render_action_email(
            eyebrow="Eyebrow",
            heading="Heading",
            body="Body",
            cta_label="Ir",
            cta_url='https://example.com/?x="><script>alert(1)</script>',
            footnote=None,
        )
        assert '"><script>alert(1)</script>' not in html
        assert "&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_escapes_footnote(self) -> None:
        html, _ = render_action_email(
            eyebrow="Eyebrow",
            heading="Heading",
            body="Body",
            cta_label="Ir",
            cta_url="https://example.com",
            footnote="<i>Nota</i>",
        )
        assert "<i>Nota</i>" not in html
        assert "&lt;i&gt;Nota&lt;/i&gt;" in html


class TestRenderActionEmailCtaBlock:
    def test_omits_cta_block_when_cta_url_is_none(self) -> None:
        html, _ = render_action_email(
            eyebrow="Eyebrow",
            heading="Heading",
            body="Body",
            cta_label="Ir al sitio",
            cta_url=None,
            footnote=None,
        )
        assert "<a href=" not in html
        assert "Ir al sitio" not in html
        assert "O copiá este link en tu navegador" not in html

    def test_includes_cta_block_when_cta_url_present(self) -> None:
        html, _ = render_action_email(
            eyebrow="Eyebrow",
            heading="Heading",
            body="Body",
            cta_label="Ir al sitio",
            cta_url="https://example.com/action",
            footnote=None,
        )
        assert '<a href="https://example.com/action"' in html
        assert "Ir al sitio" in html
        assert "O copiá este link en tu navegador" in html
        assert "https://example.com/action" in html

    def test_omits_footnote_block_when_footnote_is_none(self) -> None:
        html, _ = render_action_email(
            eyebrow="Eyebrow",
            heading="Heading",
            body="Body",
            cta_label="Ir",
            cta_url="https://example.com",
            footnote=None,
        )
        # Solo debe aparecer el estilo del footnote si hay footnote.
        assert "margin:24px 0 0 0;font-size:12px" not in html


class TestRenderActionEmailPlainText:
    def test_plain_text_contains_url_when_cta_present(self) -> None:
        _, plain = render_action_email(
            eyebrow="Eyebrow",
            heading="Heading",
            body="Body text",
            cta_label="Ir",
            cta_url="https://example.com/action?token=abc123",
            footnote=None,
        )
        assert "https://example.com/action?token=abc123" in plain

    def test_plain_text_omits_url_section_when_cta_url_none(self) -> None:
        _, plain = render_action_email(
            eyebrow="Eyebrow",
            heading="Heading",
            body="Body text",
            cta_label=None,
            cta_url=None,
            footnote=None,
        )
        assert "Copiá este link en tu navegador" not in plain

    def test_plain_text_includes_heading_body_and_footnote(self) -> None:
        _, plain = render_action_email(
            eyebrow="Eyebrow",
            heading="Mi título",
            body="Mi cuerpo",
            cta_label="Ir",
            cta_url="https://example.com",
            footnote="Mi nota al pie",
        )
        assert "Mi título" in plain
        assert "Mi cuerpo" in plain
        assert "Mi nota al pie" in plain
