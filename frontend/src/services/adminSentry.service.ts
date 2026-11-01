import { api } from "@/lib/api";

export interface SentryIssue {
  id: string;
  /** Identificador legible, p. ej. `PYTHON-FASTAPI-B`. */
  short_id: string;
  title: string;
  project: string;
  /** Tag `service` (api / worker / mcp) cuando el evento lo trae. */
  service: string | null;
  level: string | null;
  events: number;
  first_seen: string | null;
  last_seen: string | null;
  url: string;
}

export interface SentryIssuesResponse {
  /** `false` = falta el token u organización: NO significa «cero errores». */
  configured: boolean;
  dias: number;
  projects: string[];
  issues: SentryIssue[];
  total: number;
}

export const adminSentryService = {
  async issues(): Promise<SentryIssuesResponse> {
    const res = await api.get<SentryIssuesResponse>("/admin/sentry/issues");
    return res.data;
  },
};

const MENSAJES: Record<string, string> = {
  SENTRY_TOKEN_REJECTED: "Sentry rechazó el token. Revisá SENTRY_AUTH_TOKEN en Railway.",
  SENTRY_UNREACHABLE: "No se pudo conectar con Sentry. Intentá de nuevo en un rato.",
  SENTRY_BAD_RESPONSE: "Sentry respondió algo inesperado. Intentá de nuevo en un rato.",
};

/** Texto para el código estable del 502; cae a un genérico si es otro error. */
export function sentryErrorMessage(error: unknown): string {
  const detalle = (error as { response?: { data?: { detail?: { code?: string } } } } | undefined)
    ?.response?.data?.detail;
  const codigo = detalle && typeof detalle === "object" ? detalle.code : undefined;
  return (codigo && MENSAJES[codigo]) || "No se pudieron cargar los errores de Sentry.";
}
