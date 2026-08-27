import * as Sentry from "@sentry/nextjs";
import axios, { type AxiosError, type InternalAxiosRequestConfig } from "axios";
import { useAuthStore } from "@/stores/authStore";
import { usePinGateStore } from "@/stores/pinGateStore";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const api = axios.create({
  baseURL: `${BASE_URL}/api/v1`,
  headers: { "Content-Type": "application/json" },
  timeout: 15_000,
});

// Single-flight: todos los 401 simultáneos comparten el mismo promise de refresh
let _refreshPromise: Promise<string> | null = null;

api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  if (typeof window !== "undefined") {
    const token = useAuthStore.getState().token;
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
  }
  // Trazabilidad: una correlación por request (frontend → API → audit → response).
  if (!config.headers["X-Trace-Id"]) {
    config.headers["X-Trace-Id"] = crypto.randomUUID();
  }
  Sentry.addBreadcrumb({
    category: "http",
    message: `${config.method?.toUpperCase()} ${config.url}`,
    data: { trace_id: config.headers["X-Trace-Id"] },
    level: "info",
  });
  return config;
});

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as
      | (InternalAxiosRequestConfig & { _retry?: boolean; _pinRetry?: boolean })
      | undefined;

    if (!error.response) {
      // Error de red, timeout, DNS o CORS bloqueado: el backend nunca llegó a
      // procesar la request, así que nunca la reportó a su propio Sentry —
      // sin este evento, quedaría invisible en los dos proyectos. `beforeSend`
      // (scrubSentryEvent) se encarga de sanear `error.config` (headers/body).
      Sentry.captureException(error, {
        tags: { vektor_trace_id: originalRequest?.headers?.["X-Trace-Id"] as string | undefined },
      });
      return Promise.reject(error);
    }

    if (error.response.status >= 500) {
      // El backend ya generó su propio evento en `unhandled_exception_handler`
      // y la traza distribuida (CORS + sentry-trace/baggage) ya vincula esta
      // transacción con la del backend — no fabricar un segundo error acá,
      // solo dejar la referencia para poder cruzarlos.
      Sentry.addBreadcrumb({
        category: "http",
        message: `5xx: ${originalRequest?.method?.toUpperCase()} ${originalRequest?.url}`,
        data: { vektor_trace_id: error.response.headers?.["x-trace-id"] },
        level: "error",
      });
    }

    // Step-up PIN: el backend devuelve 428 con detail "PIN_REQUIRED" cuando la
    // ventana de PIN venció. Abrimos el modal (single-flight) y reintentamos UNA vez.
    if (
      error.response?.status === 428 &&
      (error.response.data as { detail?: string } | undefined)?.detail === "PIN_REQUIRED" &&
      typeof window !== "undefined" &&
      originalRequest &&
      !originalRequest._pinRetry &&
      !originalRequest.url?.includes("/auth/pin/")
    ) {
      originalRequest._pinRetry = true;
      try {
        await usePinGateStore.getState().requirePin("verify");
      } catch {
        // El usuario canceló el modal: rechazar la acción original sin reintentar.
        return Promise.reject(error);
      }
      return api.request(originalRequest);
    }

    if (
      error.response?.status === 401 &&
      typeof window !== "undefined" &&
      originalRequest &&
      !originalRequest._retry &&
      !originalRequest.url?.includes("/auth/refresh")
    ) {
      const { refreshToken, setTokens, logout } = useAuthStore.getState();

      if (!refreshToken) {
        logout();
        return Promise.reject(error);
      }

      originalRequest._retry = true;

      try {
        if (!_refreshPromise) {
          _refreshPromise = axios
            .post<{
              access_token: string;
              refresh_token: string;
              token_type: "bearer";
              expires_in: number;
            }>(`${BASE_URL}/api/v1/auth/refresh`, { refresh_token: refreshToken })
            .then((r) => {
              setTokens(r.data.access_token, r.data.refresh_token);
              return r.data.access_token;
            })
            .finally(() => {
              _refreshPromise = null;
            });
        }

        const newToken = await _refreshPromise;
        originalRequest.headers.Authorization = `Bearer ${newToken}`;
        return api.request(originalRequest);
      } catch (refreshError) {
        logout();
        return Promise.reject(refreshError);
      }
    }

    if (error.response?.status === 401 && typeof window !== "undefined") {
      useAuthStore.getState().logout();
    }
    return Promise.reject(error);
  },
);
