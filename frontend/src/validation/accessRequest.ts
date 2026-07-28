/**
 * Validación del formulario público de solicitud de acceso.
 *
 * Espejo de `backend/app/schemas/access_request.py::CreateAccessRequestRequest`.
 * Tres reglas del contrato que se replican acá a propósito, porque el backend
 * las rechaza con 422 y el usuario tiene que verlas antes de mandar:
 *
 * 1. **No hay campo `password`.** Este formulario no crea una cuenta: manda una
 *    solicitud que el dueño revisa a mano. El schema del backend usa
 *    `extra="forbid"`, así que mandar `password` es un 422 ruidoso — no un alta
 *    silenciosa.
 * 2. **`consent` es obligatorio y literalmente `true`.** El modelo persiste
 *    `consent_accepted_at NOT NULL`: la casilla no es decorativa, su valor
 *    viaja en el payload.
 * 3. **`vertical_other_text` se valida NO-VACÍO**, no solo no-nulo — y solo
 *    corresponde cuando el rubro es `otros`. Con otro rubro, mandarlo también
 *    es 422.
 */

import { z } from "zod";

import {
  CAN_SHARE_FILES_OPTIONS,
  HISTORY_DEPTH_OPTIONS,
  MAIN_CONCERN_OPTIONS,
  RECORDS_FORMAT_OPTIONS,
  REQUESTED_PLAN_OPTIONS,
  REVENUE_BAND_OPTIONS,
  STAFF_SIZE_OPTIONS,
  YEARS_OPERATING_OPTIONS,
  valuesOf,
} from "@/lib/accessRequestOptions";
import { validateOptionalPhone } from "@/lib/fiscal";
import { REQUESTED_VERTICAL_CODES, type RequestedVertical } from "@/lib/verticals";

/**
 * Largo mínimo (ya recortado) del "contanos de qué es tu negocio". Igual que
 * `MIN_VERTICAL_OTHER_TEXT` del backend: es la única información que justifica
 * haber elegido "Otro".
 */
export const MIN_VERTICAL_OTHER_TEXT = 3;

const ELEGI_UNA_OPCION = { errorMap: () => ({ message: "Elegí una opción" }) };

export const accessRequestSchema = z
  .object({
    // ── Contacto ─────────────────────────────────────────────────────────────
    full_name: z
      .string()
      .trim()
      .min(2, "Escribí tu nombre y apellido")
      .max(200, "Máximo 200 caracteres"),
    email: z.string().trim().email("Email inválido").max(255, "Máximo 255 caracteres"),
    phone: z
      .string()
      .max(50, "Máximo 50 caracteres")
      .optional()
      .superRefine((v, ctx) => {
        const error = validateOptionalPhone(v);
        if (error) ctx.addIssue({ code: z.ZodIssueCode.custom, message: error });
      }),
    business_name: z
      .string()
      .trim()
      .min(2, "Escribí el nombre de tu negocio")
      .max(200, "Máximo 200 caracteres"),

    // ── Rubro declarado ──────────────────────────────────────────────────────
    requested_vertical: z.enum(
      REQUESTED_VERTICAL_CODES as unknown as [RequestedVertical, ...RequestedVertical[]],
      { errorMap: () => ({ message: "Elegí tu rubro" }) },
    ),
    vertical_other_text: z.string().max(2000, "Máximo 2000 caracteres").optional(),

    // ── Intención de plan (obligatoria, sin preselección) ─────────────────────
    requested_plan: z.enum(valuesOf(REQUESTED_PLAN_OPTIONS), {
      errorMap: () => ({ message: "Elegí con qué cuenta querés comenzar" }),
    }),

    // ── Screening del negocio ────────────────────────────────────────────────
    years_operating: z.enum(valuesOf(YEARS_OPERATING_OPTIONS), ELEGI_UNA_OPCION),
    staff_size: z.enum(valuesOf(STAFF_SIZE_OPTIONS), ELEGI_UNA_OPCION),
    monthly_revenue_band: z.enum(valuesOf(REVENUE_BAND_OPTIONS), ELEGI_UNA_OPCION),
    main_concern: z.enum(valuesOf(MAIN_CONCERN_OPTIONS), ELEGI_UNA_OPCION),
    records_format: z.enum(valuesOf(RECORDS_FORMAT_OPTIONS), ELEGI_UNA_OPCION),
    history_depth: z.enum(valuesOf(HISTORY_DEPTH_OPTIONS), ELEGI_UNA_OPCION),
    can_share_files: z.enum(valuesOf(CAN_SHARE_FILES_OPTIONS), ELEGI_UNA_OPCION),
    records_notes: z.string().max(2000, "Máximo 2000 caracteres").optional(),
    applicant_notes: z.string().max(2000, "Máximo 2000 caracteres").optional(),

    // ── Consentimiento (Ley 25.326) ──────────────────────────────────────────
    consent: z.literal(true, {
      errorMap: () => ({ message: "Necesitamos tu consentimiento para revisar la solicitud" }),
    }),
  })
  .superRefine((data, ctx) => {
    const texto = (data.vertical_other_text ?? "").trim();
    if (data.requested_vertical === "otros") {
      if (texto.length < MIN_VERTICAL_OTHER_TEXT) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["vertical_other_text"],
          message: "Elegiste 'Otro': contanos de qué es tu negocio.",
        });
      }
      return;
    }
    // Con un rubro soportado el backend rechaza el texto libre. Nunca debería
    // llegar acá (la UI limpia el campo al cambiar de rubro), pero si llegara
    // es mejor un error visible que un 422 sin explicación.
    if (texto.length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["vertical_other_text"],
        message: "Ese texto solo corresponde cuando el rubro es 'Otro'.",
      });
    }
  });

export type AccessRequestInput = z.infer<typeof accessRequestSchema>;

/**
 * Borrador editable del formulario: todos los enums arrancan en `""` (sin
 * preselección) y los textos en `""`. No es el payload: `accessRequestSchema`
 * lo valida y `buildAccessRequestPayload` lo traduce.
 */
export interface AccessRequestDraft {
  full_name: string;
  email: string;
  phone: string;
  business_name: string;
  requested_vertical: RequestedVertical | "";
  vertical_other_text: string;
  requested_plan: string;
  years_operating: string;
  staff_size: string;
  monthly_revenue_band: string;
  main_concern: string;
  records_format: string;
  history_depth: string;
  can_share_files: string;
  records_notes: string;
  applicant_notes: string;
  consent: boolean;
}

/** Borrador vacío. Ningún campo arranca preseleccionado (no-invention). */
export const EMPTY_ACCESS_REQUEST_DRAFT: AccessRequestDraft = {
  full_name: "",
  email: "",
  phone: "",
  business_name: "",
  requested_vertical: "",
  vertical_other_text: "",
  requested_plan: "",
  years_operating: "",
  staff_size: "",
  monthly_revenue_band: "",
  main_concern: "",
  records_format: "",
  history_depth: "",
  can_share_files: "",
  records_notes: "",
  applicant_notes: "",
  consent: false,
};

/** Resultado del parseo del borrador, listo para consumir desde la UI. */
export type AccessRequestParse = z.SafeParseReturnType<unknown, AccessRequestInput>;

/** Valida un borrador contra el schema. */
export function parseAccessRequestDraft(draft: AccessRequestDraft): AccessRequestParse {
  return accessRequestSchema.safeParse(draft);
}

/** Mapa campo → primer mensaje de error, para pintar la UI. */
export function fieldErrors(parse: AccessRequestParse): Record<string, string> {
  if (parse.success) return {};
  const errores: Record<string, string> = {};
  for (const issue of parse.error.issues) {
    const campo = String(issue.path[0] ?? "");
    if (campo && !(campo in errores)) errores[campo] = issue.message;
  }
  return errores;
}
