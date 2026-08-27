import type { ErrorEvent, EventHint } from "@sentry/nextjs";
import { scrubSentryEvent } from "@/lib/sentryScrub";

function baseEvent(overrides: Partial<ErrorEvent> = {}): ErrorEvent {
  return {
    request: {},
    extra: {},
    breadcrumbs: [],
    ...overrides,
  } as ErrorEvent;
}

describe("scrubSentryEvent", () => {
  it("redacta el header Authorization del request", () => {
    const event = baseEvent({
      request: { headers: { Authorization: "Bearer secreto123" } },
    });

    const scrubbed = scrubSentryEvent(event, {} as EventHint);

    expect(scrubbed?.request?.headers?.Authorization).toBe("[Filtered]");
  });

  it("redacta extra por nombre de clave sensible", () => {
    const event = baseEvent({ extra: { monto: 15000.5, endpoint: "/sales" } });

    const scrubbed = scrubSentryEvent(event, {} as EventHint);

    expect(scrubbed?.extra?.monto).toBe("[Filtered]");
    expect(scrubbed?.extra?.endpoint).toBe("/sales");
  });

  it("redacta un CUIT aunque la clave sea genérica", () => {
    const event = baseEvent({ extra: { value: "20-12345678-9" } });

    const scrubbed = scrubSentryEvent(event, {} as EventHint);

    expect(scrubbed?.extra?.value).toBe("[Filtered]");
  });

  it("redacta breadcrumbs.data por el mismo criterio", () => {
    const event = baseEvent({
      breadcrumbs: [{ data: { email: "cliente@example.com" } }],
    });

    const scrubbed = scrubSentryEvent(event, {} as EventHint);

    expect(scrubbed?.breadcrumbs?.[0]?.data?.email).toBe("[Filtered]");
  });

  it("adjunta config.headers/data de un AxiosError redactados como contexto, sin mutar el original", () => {
    const event = baseEvent();
    const originalConfig = {
      headers: { Authorization: "Bearer secreto123" },
      data: JSON.stringify({ monto: 15000, customer_name: "Kiosco Don Pedro" }),
    };
    const hint = {
      originalException: { isAxiosError: true, config: originalConfig },
    } as unknown as EventHint;

    const scrubbed = scrubSentryEvent(event, hint);

    const axiosContext = scrubbed?.contexts?.axios_request as {
      headers: Record<string, unknown>;
      data: unknown;
    };
    expect(axiosContext.headers.Authorization).toBe("[Filtered]");
    expect(axiosContext.data).toBe("[Filtered]");
    // El AxiosError real (que el `catch` de la app todavía puede referenciar)
    // no se toca — mutarlo filtraría este redactado al resto del código.
    expect(originalConfig.headers.Authorization).toBe("Bearer secreto123");
    expect(originalConfig.data).toContain("15000");
  });

  it("no toca datos no sensibles", () => {
    const event = baseEvent({ extra: { status_code: 500, endpoint: "/sales" } });

    const scrubbed = scrubSentryEvent(event, {} as EventHint);

    expect(scrubbed?.extra).toEqual({ status_code: 500, endpoint: "/sales" });
  });
});
