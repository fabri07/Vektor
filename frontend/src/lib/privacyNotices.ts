/**
 * Avisos de confidencialidad que Véktor le muestra al usuario cuando le pide
 * datos sensibles del negocio.
 *
 * Este texto NO es relleno legal: la mayoría de los negocios chicos en
 * Argentina no declara todo lo que factura, así que preguntar por facturación
 * mensual genera desconfianza real. Decir explícitamente que no auditamos, no
 * reportamos a ARCA y no compartimos con terceros —y pedir los números reales—
 * es lo que hace que la respuesta sirva. **No editar sin subir
 * `CONSENT_NOTICE_VERSION`.**
 *
 * `FISCAL_PRIVACY_NOTE` (`lib/fiscalCondition.ts`) se mantiene aparte: es la
 * nota del selector de régimen fiscal del onboarding post-login y tiene otro
 * alcance.
 */

/**
 * Cuerpo común del aviso. Se muestra en las DOS superficies que piden números:
 * el formulario público de solicitud de acceso (arriba de la facturación) y el
 * bloque de números del onboarding post-login (arriba del régimen fiscal).
 *
 * Lo que vale para las dos vive acá; lo que solo vale para una, NO — por más
 * que cueste una línea duplicada. Ver `NOTA_FACTURACION_OPCIONAL`.
 */
export const CONFIDENTIALITY_NOTICE = {
  title: "Esta información es confidencial",
  paragraphs: [
    "Te preguntamos esto para entender tu negocio y analizar la información de la manera más eficiente.",
    "Véktor no audita, no reporta a ARCA ni comparte tu información con ningún organismo ni con terceros. No vendemos tus datos. Contestanos con los números reales de tu negocio.",
    "Cuidamos tu información siguiendo la Ley 25.326 de Protección de Datos Personales de Argentina —autoridad de aplicación: AAIP— y las normativas de protección de datos de la región.",
  ],
} as const;

/**
 * Escape de la pregunta de facturación. **Solo el formulario público.**
 *
 * No está en `paragraphs` porque el aviso lo comparten dos pantallas con reglas
 * distintas: en el formulario público la banda de facturación tiene la opción
 * `no_contesta`, pero en el onboarding post-login el campo equivalente es
 * `weekly_sales_estimate_ars`, que el backend exige con `Field(gt=0)` y no
 * tiene ningún "prefiero no decirlo". Mostrarla ahí prometería una salida que
 * ese formulario no da — y en un aviso cuyo único propósito es generar
 * confianza, una promesa incumplida hace más daño que no decir nada.
 */
export const NOTA_FACTURACION_OPCIONAL =
  'La pregunta de facturación es opcional: si preferís no contestarla, elegí "prefiero no decirlo".';

/**
 * Versión del texto de consentimiento que el formulario declara haber mostrado.
 * Tiene que coincidir con `backend/app/domain/access_request.py::CONSENT_VERSION`.
 * El backend persiste SIEMPRE la suya; esta viaja solo para auditar el contrato.
 *
 * `v2`: reescritura del cuerpo del aviso y separación de la nota de facturación,
 * que antes se mostraba también en el onboarding, donde es falsa.
 */
export const CONSENT_NOTICE_VERSION = "v2";
