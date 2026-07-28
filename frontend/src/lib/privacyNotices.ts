/**
 * Avisos de confidencialidad que Véktor le muestra al usuario cuando le pide
 * datos sensibles del negocio.
 *
 * Este texto NO es relleno legal: la mayoría de los negocios chicos en
 * Argentina no declara todo lo que factura, así que preguntar por facturación
 * mensual genera desconfianza real. Decir explícitamente que no auditamos, no
 * reportamos a ARCA y no compartimos con terceros —y pedir los números reales,
 * no los declarados— es lo que hace que la respuesta sirva. **No editar sin
 * subir `CONSENT_NOTICE_VERSION`.**
 *
 * `FISCAL_PRIVACY_NOTE` (`lib/fiscalCondition.ts`) se mantiene aparte: es la
 * nota del selector de régimen fiscal del onboarding post-login y tiene otro
 * alcance.
 */

/**
 * Aviso del bloque de números financieros. Se muestra en el formulario público
 * de solicitud de acceso (arriba de la facturación mensual) y en el bloque de
 * números del onboarding post-login (arriba del régimen fiscal).
 */
export const CONFIDENTIALITY_NOTICE = {
  title: "Esta información es confidencial",
  paragraphs: [
    "Te preguntamos esto para entender tu negocio y darte números que te sirvan — nada más.",
    "Véktor no audita, no reporta a ARCA (ex-AFIP) ni comparte tu información con ningún organismo ni con terceros. No vendemos tus datos. Contestanos con los números reales de tu negocio, no con los declarados: si no coinciden, a nosotros no nos cambia nada y a vos te sirve muchísimo más.",
    // La última frase decía "Todas estas preguntas son opcionales" y era falsa:
    // el schema del backend exige los siete campos del screening y la única con
    // escape real es la de facturación (`no_contesta`). Prometer lo que el
    // formulario no cumple destruye exactamente la confianza que este aviso
    // viene a construir.
    "Cuidamos tu información siguiendo la Ley 25.326 de Protección de Datos Personales de Argentina —autoridad de aplicación: AAIP— y las normativas de protección de datos de la región. La pregunta de facturación es opcional: si preferís no contestarla, elegí \"prefiero no decirlo\".",
  ],
} as const;

/**
 * Versión del texto de consentimiento que el formulario declara haber mostrado.
 * Tiene que coincidir con `backend/app/domain/access_request.py::CONSENT_VERSION`.
 * El backend persiste SIEMPRE la suya; esta viaja solo para auditar el contrato.
 */
export const CONSENT_NOTICE_VERSION = "v1";
