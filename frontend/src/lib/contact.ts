/**
 * Constantes compartidas del formulario público de contacto.
 *
 * `CONSENT_VERSION` debe coincidir con la del backend
 * (`app/domain/contact_lead.py::CONSENT_VERSION`). El front la envía en el
 * payload para que el contrato sea auditable; el backend sigue siendo la fuente
 * autoritativa (si difiere, usa la suya).
 */

export const CONSENT_VERSION = "2026-07-v1";
