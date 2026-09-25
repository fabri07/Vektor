/**
 * B6 — un corte de red no desloguea, y la credencial de la caja viaja sólo
 * cuando corresponde.
 */
import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";

jest.mock("@sentry/nextjs", () => ({
  captureException: jest.fn(),
  addBreadcrumb: jest.fn(),
}));

import { api, handleApiResponseError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { usePosTerminalStore } from "@/stores/posTerminalStore";

const logoutSpy = jest.fn();

function error401(url = "/sales"): AxiosError {
  return {
    isAxiosError: true,
    config: { url, method: "get", headers: {} },
    response: { status: 401, data: {}, headers: {}, statusText: "", config: {} },
    name: "AxiosError",
    message: "401",
    toJSON: () => ({}),
  } as unknown as AxiosError;
}

function fallaDeRefresh(status: number | null): AxiosError {
  const e = new AxiosError(status ? `status ${status}` : "Network Error");
  if (status) {
    e.response = {
      status,
      data: {},
      headers: {},
      statusText: "",
      config: {} as InternalAxiosRequestConfig,
    };
  }
  return e;
}

beforeEach(() => {
  jest.restoreAllMocks();
  logoutSpy.mockReset();
  useAuthStore.setState({
    token: "viejo",
    refreshToken: "r1",
    user: { id: "u1", email: "a@b.com", full_name: "Ana", role: "OWNER", tenant_id: "t1" },
    logout: logoutSpy,
  });
  usePosTerminalStore.setState({ terminal: null, invalid: false });
});

describe("refresh del token", () => {
  it("un error de red NO desloguea y conserva los tokens", async () => {
    jest.spyOn(axios, "post").mockRejectedValue(fallaDeRefresh(null));
    await expect(handleApiResponseError(error401())).rejects.toBeTruthy();
    expect(logoutSpy).not.toHaveBeenCalled();
    expect(useAuthStore.getState().refreshToken).toBe("r1");
  });

  it("un 5xx del servidor NO desloguea", async () => {
    jest.spyOn(axios, "post").mockRejectedValue(fallaDeRefresh(503));
    await expect(handleApiResponseError(error401())).rejects.toBeTruthy();
    expect(logoutSpy).not.toHaveBeenCalled();
  });

  it("un 401 del servidor al refrescar SÍ desloguea: la sesión fue revocada", async () => {
    jest.spyOn(axios, "post").mockRejectedValue(fallaDeRefresh(401));
    await expect(handleApiResponseError(error401())).rejects.toBeTruthy();
    expect(logoutSpy).toHaveBeenCalledTimes(1);
  });

  it("dos 401 simultáneos comparten un solo refresh", async () => {
    const post = jest.spyOn(axios, "post").mockResolvedValue({
      data: { access_token: "nuevo", refresh_token: "r2", token_type: "bearer", expires_in: 60 },
    });
    jest.spyOn(api, "request").mockResolvedValue({ data: "ok" });
    await Promise.all([
      handleApiResponseError(error401("/a")),
      handleApiResponseError(error401("/b")),
    ]);
    expect(post).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().token).toBe("nuevo");
  });
});

describe("credencial de la caja", () => {
  type Handler = { fulfilled: (c: InternalAxiosRequestConfig) => InternalAxiosRequestConfig };
  const handler = (api.interceptors.request as unknown as { handlers: Handler[] }).handlers[0];
  if (!handler) throw new Error("api no tiene interceptor de request");
  const interceptor = handler.fulfilled;

  function pedir(url: string): InternalAxiosRequestConfig {
    return interceptor({ url, headers: new axios.AxiosHeaders() } as InternalAxiosRequestConfig);
  }

  it("viaja en las llamadas de caja del mismo negocio", () => {
    usePosTerminalStore
      .getState()
      .setTerminal({ terminalId: "c1", name: "Caja", secret: "s3cr3t", tenantId: "t1" });
    expect(pedir("/pos/operations").headers["X-POS-Terminal"]).toBe("s3cr3t");
    expect(pedir("/products/abc/barcode").headers["X-POS-Terminal"]).toBe("s3cr3t");
    expect(pedir("/sales").headers["X-POS-Terminal"]).toBeUndefined();
  });

  it("no viaja si el logueado es de otro negocio", () => {
    usePosTerminalStore
      .getState()
      .setTerminal({ terminalId: "c1", name: "Caja", secret: "s3cr3t", tenantId: "otro" });
    expect(pedir("/pos/operations").headers["X-POS-Terminal"]).toBeUndefined();
  });

  it("un TERMINAL_DISABLED del servidor la marca inválida", async () => {
    const e = {
      isAxiosError: true,
      config: { url: "/pos/operations", headers: {} },
      response: {
        status: 403,
        headers: {},
        statusText: "",
        config: {},
        data: { detail: { code: "TERMINAL_DISABLED" } },
      },
      toJSON: () => ({}),
    } as unknown as AxiosError;
    await expect(handleApiResponseError(e)).rejects.toBe(e);
    expect(usePosTerminalStore.getState().invalid).toBe(true);
  });
});
