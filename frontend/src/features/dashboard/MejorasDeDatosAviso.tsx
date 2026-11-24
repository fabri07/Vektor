"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  aplicarMejora,
  dispararMejorasAutomaticas,
  fetchMejorasDeDatos,
  revertirMejora,
} from "@/services/mejorasDeDatos.service";
import { useAuthStore } from "@/stores/authStore";
import { esPinCancelado } from "@/stores/pinGateStore";
import { MejoraVistaPrevia } from "./MejoraVistaPrevia";

// Quién ve el aviso: UX, no seguridad (el 403/428 del servidor decide).
const ROLES_QUE_CORRIGEN: ReadonlySet<string> = new Set(["OWNER", "ADMIN"]);
const POLLING_MS = 5_000;
const POLLING_VECES = 3;

/** El motivo del servidor (423/409 lo traen); si no hay, uno genérico. */
function mensajeDeError(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const m = (detail as { message?: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  return "No se pudo completar. Probá de nuevo.";
}

// El disparo automático es una vez por sesión de pestaña: el servidor igual lo
// acota (SET NX 10 min), esto sólo evita pedirlo en cada render del Dashboard.
let disparado = false;

/**
 * Mejoras de datos: lo que Véktor corrigió solo en lo ya cargado («Deshacer») y
 * lo que cambiaría un valor existente («Aplicar», con vista previa y PIN). Si el
 * servidor todavía no tiene la ruta (deploy en paralelo) o falla, se oculta.
 */
export function MejorasDeDatosAviso() {
  const role = useAuthStore((s) => s.user?.role);
  const puede = role != null && ROLES_QUE_CORRIGEN.has(role);
  const qc = useQueryClient();
  const [polling, setPolling] = useState(0);
  const [mensaje, setMensaje] = useState<string | null>(null);
  // La mejora cuya vista previa está abierta (una a la vez).
  const [abierta, setAbierta] = useState<string | null>(null);

  const panel = useQuery({
    queryKey: ["data-improvements"],
    queryFn: fetchMejorasDeDatos,
    enabled: puede,
    retry: false,
    refetchInterval: polling > 0 ? POLLING_MS : false,
  });

  useEffect(() => {
    if (!puede || disparado) return;
    disparado = true;
    dispararMejorasAutomaticas()
      .then((r) => {
        if (r.queued) setPolling(POLLING_VECES);
      })
      .catch(() => undefined); // accesorio: nunca rompe el Dashboard
  }, [puede]);

  // Polling acotado: no hay result backend, el estado sólo se ve releyendo.
  useEffect(() => {
    if (polling <= 0) return;
    const t = setTimeout(() => setPolling((n) => n - 1), POLLING_MS);
    return () => clearTimeout(t);
  }, [polling]);

  const refrescar = async () => {
    // Aplicar/deshacer mueve costos, gastos, caja y margen: todo lo que los lee.
    await Promise.all(
      [
        ["data-improvements"],
        ["margins"],
        ["products"],
        ["health-scores"],
        ["expenses-entries"],
        ["expenses-all"],
        ["expenses-count"],
        ["expenses-date-range"],
        ["cash-preview"],
        ["economic-summary"],
      ].map((queryKey) => qc.invalidateQueries({ queryKey })),
    );
  };

  const deshacer = useMutation({
    mutationFn: (runId: string) => revertirMejora(runId),
    onSuccess: async (r) => {
      setMensaje(
        r.completa
          ? "Listo: se deshizo la corrida."
          : `Se deshizo lo que seguía igual; ${Object.keys(r.pendientes).length} quedaron como estaban porque cambiaron después.`,
      );
      await refrescar();
    },
    onError: (e) => {
      if (!esPinCancelado(e)) setMensaje(mensajeDeError(e));
    },
  });

  const aplicar = useMutation({
    mutationFn: ({ mejoraId, ids }: { mejoraId: string; ids: string[] }) =>
      aplicarMejora(mejoraId, ids),
    onSuccess: async (r) => {
      setAbierta(null);
      setMensaje(
        `Listo: ${r.aplicadas} ${r.aplicadas === 1 ? "dato corregido" : "datos corregidos"}.` +
          (r.restantes > 0 ? ` Quedan ${r.restantes}: volvé a aplicar.` : ""),
      );
      await refrescar();
    },
    onError: (e) => {
      if (!esPinCancelado(e)) setMensaje(mensajeDeError(e));
    },
  });

  if (!puede || panel.isError || !panel.data) return null;

  const aplicadas = panel.data.aplicadas.filter((a) => a.status === "APPLIED" && a.revertible);
  const paraAplicar = panel.data.pendientes.filter(
    (p) => p.clase === "cambia_valores" && p.cantidad > 0,
  );
  if (aplicadas.length === 0 && paraAplicar.length === 0 && !mensaje) return null;

  return (
    <section
      aria-label="Mejoras de datos"
      className="mt-4 space-y-2 rounded-xl border border-vektor-border px-4 py-3"
    >
      {aplicadas.map((a) => (
        <div key={a.run_id} className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-vektor-body">
            Se completaron {a.aplicadas} {a.aplicadas === 1 ? "dato" : "datos"} de tus archivos:{" "}
            {a.titulo}.
          </p>
          <button
            type="button"
            disabled={deshacer.isPending}
            onClick={() => deshacer.mutate(a.run_id)}
            className="h-8 rounded-lg border border-vektor-border px-3 text-xs text-vektor-body disabled:opacity-40"
          >
            Deshacer
          </button>
        </div>
      ))}
      {paraAplicar.map((p) => (
        <div key={p.mejora_id} className="space-y-1">
          <p className="text-sm font-medium text-vektor-white">
            {p.necesita_archivo
              ? `${p.titulo}: hasta ${p.cantidad} ${p.cantidad === 1 ? "dato podría cambiar" : "datos podrían cambiar"} (se confirma al leer tus archivos)`
              : `${p.titulo}: ${p.cantidad} ${p.cantidad === 1 ? "dato cambiaría" : "datos cambiarían"}`}
          </p>
          {abierta === p.mejora_id ? (
            <MejoraVistaPrevia
              mejoraId={p.mejora_id}
              aplicando={aplicar.isPending}
              onAplicar={(ids) => aplicar.mutate({ mejoraId: p.mejora_id, ids })}
              onCerrar={() => setAbierta(null)}
            />
          ) : (
            <button
              type="button"
              onClick={() => setAbierta(p.mejora_id)}
              className="h-8 rounded-lg bg-vektor-blue px-3 text-xs font-medium text-white"
            >
              Ver y aplicar
            </button>
          )}
        </div>
      ))}
      {mensaje && <p className="text-xs text-vektor-muted">{mensaje}</p>}
    </section>
  );
}
