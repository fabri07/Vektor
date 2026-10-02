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
import { hasPosPermission } from "@/lib/roles";
import { posErrorInfo } from "@/lib/pos/errors";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";

/** Colores por superficie: `/products` usa los de la app; la caja, sólo los
 *  `pos-*` de su tema claro/oscuro (ver `pos_theme.test.tsx`). */
const TEMAS = {
  app: {
    seccion: "rounded-lg border border-vk-border-w bg-vk-surface-w p-4",
    titulo: "text-sm font-semibold text-vk-text-primary",
    texto: "text-vk-text-primary",
    secundario: "text-vk-text-secondary",
    tenue: "text-vk-text-muted",
    item: "rounded border border-vk-border-w p-3",
    campo:
      "w-full rounded border border-vk-border-w bg-vk-bg-light px-2 py-1 text-sm text-vk-text-primary",
  },
  caja: {
    seccion: "rounded-lg border border-pos-border bg-pos-surface p-3",
    titulo: "text-sm font-semibold text-pos-text",
    texto: "text-pos-text",
    secundario: "text-pos-text-2",
    tenue: "text-pos-muted",
    item: "rounded border border-pos-border p-3",
    campo:
      "w-full rounded border border-pos-border bg-pos-field px-2 py-1 text-sm text-pos-text",
  },
} as const;

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

interface Props {
  /** Montado dentro de la caja (para el cajero con `resolve_discrepancy`). */
  enCaja?: boolean;
}

export function StockDiscrepanciesPanel({ enCaja = false }: Props) {
  const t = TEMAS[enCaja ? "caja" : "app"];
  const qc = useQueryClient();
  const toast = useToastStore((s) => s.add);
  const [abierta, setAbierta] = useState<string | null>(null);
  const [resolucion, setResolucion] = useState<ResolucionDeDiferencia>("ajuste_de_entrada");
  const [motivo, setMotivo] = useState("");

  // Resolver una diferencia mueve stock: es del dueño o del encargado, o de un
  // cajero al que el dueño se lo delegó (`resolve_discrepancy`, con su PIN).
  // Nunca de un usuario de sólo lectura: sin este gate, la ruta contestaría
  // 403 a cada visita de /products.
  const user = useAuthStore((s) => s.user);
  const puedeVer = hasPosPermission(user, "resolve_discrepancy");
  // «La venta está mal» anula el ticket: un cajero además necesita poder anular
  // (y que el ticket sea suyo, lo que verifica el servidor). Sin el permiso,
  // ofrecerla sólo lleva a un rechazo después del motivo y el PIN.
  const opciones = OPCIONES.filter(
    (o) => o.valor !== "anular_ticket" || hasPosPermission(user, "void_ticket"),
  );
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
      void qc.invalidateQueries({ queryKey: ["products-list"] });
    },
    // El motivo del servidor: "sólo podés anular tus propios tickets" se
    // entiende; "no se pudo" no.
    onError: (e) =>
      toast(posErrorInfo(e).message ?? "No se pudo resolver la diferencia.", "error"),
  });

  if (!data || data.length === 0) return null;

  return (
    <section className={t.seccion}>
      <h2 className={t.titulo}>
        Diferencias de stock de caja ({data.length})
      </h2>
      <p className={`mt-1 text-xs ${t.tenue}`}>
        Se cobraron sin conexión y al sincronizar el stock no alcanzaba. La venta entró
        completa: falta decidir qué pasó con la mercadería.
      </p>
      <ul className="mt-3 space-y-3">
        {data.map((d) => (
          <li key={d.id} className={t.item}>
            <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
              <span className={t.texto}>
                {d.product_name ?? "Producto sin nombre"} · {d.pending_units} unidades
              </span>
              <span className={`text-xs ${t.tenue}`}>
                vendidas el {fecha(d.operation_date)} · el sistema tenía {d.stock_at_capture}
              </span>
            </div>
            {abierta === d.id ? (
              <div className="mt-3 space-y-2">
                {opciones.map((o) => (
                  <label key={o.valor} className={`flex gap-2 text-sm ${t.secundario}`}>
                    <input
                      type="radio"
                      name={`resolucion-${d.id}`}
                      checked={resolucion === o.valor}
                      onChange={() => setResolucion(o.valor)}
                    />
                    <span>
                      <span className={`font-medium ${t.texto}`}>{o.titulo}</span>
                      <br />
                      <span className={`text-xs ${t.tenue}`}>{o.detalle}</span>
                    </span>
                  </label>
                ))}
                <input
                  type="text"
                  aria-label="Motivo"
                  placeholder="¿Por qué? (queda en la auditoría)"
                  value={motivo}
                  onChange={(e) => setMotivo(e.target.value)}
                  className={t.campo}
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
