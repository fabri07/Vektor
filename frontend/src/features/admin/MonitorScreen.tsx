"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { EmptyState } from "@/components/ui/EmptyState";
import { StatCard } from "@/components/ui/StatCard";
import { adminMonitorService } from "@/services/adminMonitor.service";
import type { MonitorAtencion, MotivoDeAtencion } from "@/services/adminMonitor.service";
import { SentryErrorsBlock } from "./SentryErrorsBlock";
import { diaCorto, entero, fechaCorta } from "./format";

const MOTIVOS: Record<MotivoDeAtencion, { rotulo: string; fecha: string }> = {
  READ_ONLY: { rotulo: "Suscripción vencida (solo lectura)", fecha: "Venció" },
  GRACE: { rotulo: "En período de gracia", fecha: "La gracia termina" },
  TRIAL_ENDING: { rotulo: "La prueba está por terminar", fecha: "La prueba termina" },
  INACTIVE: { rotulo: "Sin uso en los últimos 14 días", fecha: "Último uso" },
};

/** `fecha` llega como día (`YYYY-MM-DD`) o como fecha-hora ISO según el motivo. */
function fechaDelMotivo(fecha: string | null): string {
  if (!fecha) return "nunca";
  return /^\d{4}-\d{2}-\d{2}$/.test(fecha) ? diaCorto(fecha) : fechaCorta(fecha);
}

function fmtUsd(valor: number): string {
  return `$${(Number.isFinite(valor) ? valor : 0).toFixed(2)}`;
}

function Bloque({ titulo, children }: { titulo: string; children: React.ReactNode }) {
  return (
    <section className="space-y-3">
      <h2 className="text-sm font-semibold text-vk-text-primary">{titulo}</h2>
      {children}
    </section>
  );
}

function FilaAtencion({ item }: { item: MonitorAtencion }) {
  const m = MOTIVOS[item.motivo];
  return (
    <li>
      <Link
        href={`/admin/tenants/${item.tenant_id}`}
        className="flex flex-wrap items-baseline justify-between gap-2 rounded-lg border border-vk-border bg-vk-surface-w px-4 py-3 transition-colors hover:border-vk-blue/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-vk-blue/60"
      >
        <span className="font-medium text-vk-text-primary">{item.display_name}</span>
        <span className="text-sm text-vk-text-muted">
          {m.rotulo} · {m.fecha}: {fechaDelMotivo(item.fecha)}
        </span>
      </Link>
    </li>
  );
}

export function MonitorScreen() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["admin-monitor"],
    queryFn: () => adminMonitorService.get(),
    staleTime: 60 * 1000,
  });

  if (isLoading) {
    return (
      <PageWrapper title="Monitor de la plataforma">
        <p className="text-sm text-vk-text-muted">Cargando…</p>
      </PageWrapper>
    );
  }
  if (isError || !data) {
    return (
      <PageWrapper title="Monitor de la plataforma">
        <EmptyState
          title="No se pudo cargar el monitor"
          description="Ocurrió un error al consultar la plataforma. Intentá nuevamente."
        />
      </PageWrapper>
    );
  }

  const { negocios, suscripciones, actividad, ia, atencion, atencion_total } = data;

  return (
    <PageWrapper title="Monitor de la plataforma">
      <div className="space-y-8">
        <Bloque titulo="Negocios (sin demos)">
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <StatCard label="Total" value={entero(negocios.total)} />
            <StatCard label="Activos" value={entero(negocios.active)} />
            <StatCard label="En prueba" value={entero(negocios.trial)} />
            <StatCard label="Solo lectura" value={entero(negocios.read_only)} />
            <StatCard label="Suspendidos" value={entero(negocios.suspended)} />
          </div>
        </Bloque>

        <Bloque titulo="Suscripciones vivas">
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-6">
            <StatCard label="En prueba" value={entero(suscripciones.trial)} />
            <StatCard label="Activas" value={entero(suscripciones.active)} />
            <StatCard label="En gracia" value={entero(suscripciones.grace)} />
            <StatCard label="Solo lectura" value={entero(suscripciones.read_only)} />
            <StatCard label="Vencen en 7 días" value={entero(suscripciones.vencen_en_7_dias)} />
            <StatCard label="Sin suscripción" value={entero(suscripciones.sin_suscripcion)} />
          </div>
          {suscripciones.sin_suscripcion > 0 && (
            <p className="text-xs text-vk-warning">
              Un negocio sin suscripción viva recibe error 503: revisalo en Negocios.
            </p>
          )}
        </Bloque>

        <Bloque titulo="Actividad (negocios con uso)">
          <div className="grid grid-cols-3 gap-4">
            <StatCard label="Hoy" value={entero(actividad.hoy)} />
            <StatCard label="Últimos 7 días" value={entero(actividad.ultimos_7_dias)} />
            <StatCard label="Últimos 30 días" value={entero(actividad.ultimos_30_dias)} />
          </div>
        </Bloque>

        <Bloque titulo={`Atención requerida${atencion_total > 0 ? ` (${atencion_total})` : ""}`}>
          {atencion.length === 0 ? (
            <p className="text-sm text-vk-text-muted">
              Ningún negocio necesita atención ahora.
            </p>
          ) : (
            <>
              <ul className="space-y-2">
                {atencion.map((a) => (
                  <FilaAtencion key={`${a.tenant_id}-${a.motivo}`} item={a} />
                ))}
              </ul>
              {atencion_total > atencion.length && (
                <p className="text-xs text-vk-text-muted">
                  Se muestran {atencion.length} de {atencion_total}.{" "}
                  <Link href="/admin/tenants" className="underline">
                    Ver todos los negocios
                  </Link>
                </p>
              )}
            </>
          )}
        </Bloque>

        <SentryErrorsBlock />

        <Bloque titulo={`IA — últimos ${ia.dias} días`}>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
            <StatCard label="Tokens" value={entero(ia.tokens_total)} />
            <StatCard label="Costo estimado (USD)" value={fmtUsd(ia.cost_usd)} />
            <StatCard
              label="Último consumo"
              value={ia.ultimo_consumo ? diaCorto(ia.ultimo_consumo) : "Nunca"}
            />
          </div>
          {ia.unpriced_tokens > 0 && (
            <p className="text-xs text-vk-warning">
              {entero(ia.unpriced_tokens)} tokens sin precio: el costo mostrado subestima el real.
            </p>
          )}
          <Link href="/admin/usage" className="text-sm underline">
            Ver detalle en Uso &amp; costos
          </Link>
        </Bloque>
      </div>
    </PageWrapper>
  );
}
