"use client";

/**
 * Las salidas de stock que quedaron pendientes por una venta de caja (B11).
 *
 * Aparece sólo si hay alguna. Véktor no las corrige solo: no sabe si la
 * mercadería estaba o si la venta está mal, y adivinar deja el stock peor. Por
 * eso hay dos resoluciones con efecto exacto, las dos piden un motivo, y no hay
 * una tercera que sólo oculte el aviso.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/Button";
import {
  type PosStockDiscrepancy,
  type ResolucionDeDiferencia,
  posService,
} from "@/services/pos.service";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";

const OPCIONES: { valor: ResolucionDeDiferencia; titulo: string; detalle: string }[] = [
  {
    valor: "ajuste_de_entrada",
    titulo: "La mercadería estaba",
    detalle:
      "El sistema no la tenía cargada. Entra lo que falta como ajuste, fechado el día de la venta, y después sale la venta completa.",
  },
  {
    valor: "anular_ticket",
    titulo: "La venta está mal",
    detalle: "Se anula el ticket entero: se revierten sus líneas, sus pagos y su stock.",
  },
];

function fecha(iso: string): string {
  return new Date(iso).toLocaleDateString("es-AR", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
  });
}

export function StockDiscrepanciesPanel() {
  const qc = useQueryClient();
  const toast = useToastStore((s) => s.add);
  const [abierta, setAbierta] = useState<string | null>(null);
  const [resolucion, setResolucion] = useState<ResolucionDeDiferencia>("ajuste_de_entrada");
  const [motivo, setMotivo] = useState("");

  // Resolver una diferencia mueve stock: es del dueño, no de la caja ni de un
  // usuario de sólo lectura. Sin este gate, la ruta contestaría 403 a cada
  // visita de /products.
  const rol = useAuthStore((s) => s.user?.role);
  const puedeVer = rol === "OWNER" || rol === "ADMIN";
  const { data } = useQuery({
    queryKey: ["pos-stock-discrepancies"],
    queryFn: () => posService.listStockDiscrepancies(true),
    enabled: puedeVer,
  });

  const resolver = useMutation({
    mutationFn: (d: PosStockDiscrepancy) =>
      posService.resolveStockDiscrepancy(d.id, resolucion, motivo.trim()),
    onSuccess: () => {
      toast("Diferencia resuelta.", "success");
      setAbierta(null);
      setMotivo("");
      void qc.invalidateQueries({ queryKey: ["pos-stock-discrepancies"] });
      void qc.invalidateQueries({ queryKey: ["products"] });
    },
    onError: () => toast("No se pudo resolver la diferencia.", "error"),
  });

  if (!data || data.length === 0) return null;

  return (
    <section className="rounded-lg border border-vk-border-w bg-vk-surface-w p-4">
      <h2 className="text-sm font-semibold text-vk-text-primary">
        Diferencias de stock de caja ({data.length})
      </h2>
      <p className="mt-1 text-xs text-vk-text-muted">
        Se cobraron sin conexión y al sincronizar el stock no alcanzaba. La venta entró
        completa: falta decidir qué pasó con la mercadería.
      </p>
      <ul className="mt-3 space-y-3">
        {data.map((d) => (
          <li key={d.id} className="rounded border border-vk-border-w p-3">
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className="text-vk-text-primary">
                {d.product_name ?? "Producto sin nombre"} · {d.pending_units} unidades
              </span>
              <span className="text-xs text-vk-text-muted">
                vendidas el {fecha(d.operation_date)} · el sistema tenía {d.stock_at_capture}
              </span>
            </div>
            {abierta === d.id ? (
              <div className="mt-3 space-y-2">
                {OPCIONES.map((o) => (
                  <label key={o.valor} className="flex gap-2 text-sm text-vk-text-secondary">
                    <input
                      type="radio"
                      name={`resolucion-${d.id}`}
                      checked={resolucion === o.valor}
                      onChange={() => setResolucion(o.valor)}
                    />
                    <span>
                      <span className="font-medium text-vk-text-primary">{o.titulo}</span>
                      <br />
                      <span className="text-xs text-vk-text-muted">{o.detalle}</span>
                    </span>
                  </label>
                ))}
                <input
                  type="text"
                  aria-label="Motivo"
                  placeholder="¿Por qué? (queda en la auditoría)"
                  value={motivo}
                  onChange={(e) => setMotivo(e.target.value)}
                  className="w-full rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-sm text-vk-text-primary"
                />
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    disabled={motivo.trim() === "" || resolver.isPending}
                    onClick={() => resolver.mutate(d)}
                  >
                    Resolver
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setAbierta(null)}>
                    Cancelar
                  </Button>
                </div>
              </div>
            ) : (
              <Button
                className="mt-2"
                size="sm"
                variant="secondary"
                onClick={() => {
                  setAbierta(d.id);
                  setResolucion("ajuste_de_entrada");
                  setMotivo("");
                }}
              >
                Resolver
              </Button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
