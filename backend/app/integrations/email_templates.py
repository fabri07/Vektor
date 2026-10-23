# ruff: noqa: E501
"""Esqueleto HTML compartido para emails transaccionales de acción.

Extrae el markup que hasta ahora estaba duplicado entre
``AuthService._send_verification_email`` y ``AuthService._send_reset_email``
(tabla de 520px, header ``#1A1A2E``, botón ``#2B7FD4``). Cualquier email nuevo
que necesite el mismo esqueleto (ej. las notificaciones de solicitud de acceso)
debe usar ``render_action_email`` en lugar de duplicar markup otra vez.

Todo valor interpolado se escapa con ``html.escape()`` antes de insertarse en
el markup — mismo patrón que ``contact_lead_service.build_lead_email`` (¡ojo
con ese archivo!: ahí una variable local llamada ``html`` shadowea el import
del módulo ``html``, que es exactamente el bug que este archivo evita no
nombrando ninguna variable local ``html``).
"""

from __future__ import annotations

import html


def render_action_email(
    *,
    eyebrow: str,
    heading: str,
    body: str,
    cta_label: str | None,
    cta_url: str | None,
    footnote: str | None,
) -> tuple[str, str]:
    """Arma ``(html, texto_plano)`` de un email transaccional de acción.

    - ``eyebrow``: texto chico debajo de "Véktor" en el header oscuro.
    - ``heading``: título en negrita del cuerpo.
    - ``body``: párrafo descriptivo debajo del título.
    - ``cta_label``/``cta_url``: botón de acción. Si ``cta_url`` es ``None``
      se omite el botón entero Y el bloque de "copiá este link" del pie (no
      hay URL que mostrar).
    - ``footnote``: párrafo chico gris debajo del botón (opcional).

    Todos los valores se escapan con ``html.escape()`` antes de interpolarse.
    """
    safe_eyebrow = html.escape(eyebrow)
    safe_heading = html.escape(heading)
    safe_body = html.escape(body)
    safe_footnote = html.escape(footnote) if footnote else None

    cta_html = ""
    copy_link_row = ""
    if cta_url:
        safe_cta_url = html.escape(cta_url)
        safe_cta_label = html.escape(cta_label) if cta_label else ""
        cta_html = f"""
              <a href="{safe_cta_url}"
                 style="display:inline-block;background-color:#2B7FD4;color:#ffffff;font-size:14px;font-weight:600;
                        text-decoration:none;padding:12px 28px;border-radius:8px;letter-spacing:0.01em;">
                {safe_cta_label}
              </a>"""
        copy_link_row = f"""
          <tr>
            <td style="padding:16px 32px 24px 32px;border-top:1px solid #f3f4f6;">
              <p style="margin:0;font-size:11px;color:#9ca3af;">
                O copiá este link en tu navegador:<br/>
                <span style="color:#6b7280;word-break:break-all;">{safe_cta_url}</span>
              </p>
            </td>
          </tr>"""

    footnote_html = ""
    if safe_footnote:
        footnote_html = f"""
              <p style="margin:24px 0 0 0;font-size:12px;color:#9ca3af;">
                {safe_footnote}
              </p>"""

    rendered_html = f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f9fafb;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="520" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb;">
          <tr>
            <td style="background-color:#1A1A2E;padding:28px 32px;">
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;letter-spacing:-0.02em;">Véktor</p>
              <p style="margin:4px 0 0 0;font-size:13px;color:rgba(255,255,255,0.5);">{safe_eyebrow}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <p style="margin:0 0 16px 0;font-size:16px;font-weight:600;color:#111827;">{safe_heading}</p>
              <p style="margin:0 0 24px 0;font-size:14px;color:#6b7280;line-height:1.6;">
                {safe_body}
              </p>{cta_html}{footnote_html}
            </td>
          </tr>{copy_link_row}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    plain_lines = [heading, "", body]
    if cta_url:
        plain_lines += ["", "Copiá este link en tu navegador:", cta_url]
    if footnote:
        plain_lines += ["", footnote]
    plain_text = "\n".join(plain_lines)

    return rendered_html, plain_text
