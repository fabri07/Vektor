"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Users } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Modal } from "@/components/ui/Modal";
import { StatCard } from "@/components/ui/StatCard";
import { paymentLabel } from "@/lib/payment";
import { useToastStore } from "@/stores/toastStore";
import {
  CAPITAL_LIST_LIMIT,
  ownerCapitalService,
  type CapitalAmountsByForm,
  type CapitalMovement,
  type CapitalPartnerSummary,
} from "@/services/owner-capital.service";
import {
  CAPITAL_KIND_LABELS,
  CapitalMovementModal,
  invalidateCapitalQueries,
} from "@/features/economic-summary/CapitalMovementModal";

function formatARS(value: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(value);
}

/** Un monto tal cual viene del backend; la especie sólo si la pantalla la muestra. */
function Monto({ value, showInKind }: { value: CapitalAmountsByForm; showInKind: boolean }) {
  return (
    <>
      <span>{formatARS(value.dinero)}</span>
      {showInKind && value.especie !== 0 && (
        <span className="block text-xs text-vk-text-muted">
          en especie: {formatARS(value.especie)}
        </span>
      )}
    </>
  );
}

function PartnerRow({
  row,
  showInKind,
  isTotal = false,
}: {
  row: CapitalPartnerSummary;
  showInKind: boolean;
  isTotal?: boolean;
}) {
  const name = isTotal ? "Total" : row.partner_name ?? "Sin socio identificado";
  return (
    <tr className={isTotal ? "border-t border-vk-border-w font-semibold" : undefined}>
      <td className="px-3 py-2 text-left">{name}</td>
      <td className="px-3 py-2 text-right">
        <Monto value={row.aportes} showInKind={showInKind} />
      </td>
      <td className="px-3 py-2 text-right">
        <Monto value={row.retiros} showInKind={showInKind} />
      </td>
      <td className="px-3 py-2 text-right">
        <Monto value={row.prestamos_recibidos} showInKind={showInKind} />
      </td>
      <td className="px-3 py-2 text-right">
        <Monto value={row.devoluciones} showInKind={showInKind} />
      </td>
      <td
        className={
          row.prestamo_pendiente_inconsistente
            ? "px-3 py-2 text-right text-vk-danger"
            : "px-3 py-2 text-right"
        }
      >
        {formatARS(row.prestamo_pendiente)}
      </td>
    </tr>
  );
}

function VoidMovementModal({
  movement,
  onClose,
}: {
  movement: CapitalMovement | null;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  useEffect(() => {
    setReason("");
    setError(null);
  }, [movement]);

  const mutation = useMutation({
    mutationFn: (m: CapitalMovement) => ownerCapitalService.void(m.id, reason.trim()),
    onSuccess: async () => {
      toast("Movimiento anulado.", "success");
      await invalidateCapitalQueries(queryClient);
      onClose();
    },
    onError: () => toast("No se pudo anular el movimiento.", "error"),
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!movement) return;
    if (!reason.trim()) {
      setError("Contá por qué lo anulás.");
      return;
    }
    setError(null);
    mutation.mutate(movement);
  }

  return (
    <Modal isOpen={movement !== null} onClose={onClose} title="Anular movimiento" size="sm">
      <form className="space-y-4" onSubmit={handleSubmit}>
        <p className="text-sm text-vk-text-secondary">
          El movimiento no se borra: queda registrado como anulado y deja de contar.
        </p>
        <Input
          label="Motivo"
          type="text"
          placeholder="Ej: lo cargué dos veces"
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        {error && (
          <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-3 py-2 text-sm text-vk-danger">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" size="sm" variant="danger" loading={mutation.isPending}>
            Anular
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/**
 * Capital de los socios en el período (D·4). Sólo se monta para OWNER/ADMIN.
 * Todos los números vienen calculados del backend (FactsService): acá no se
 * suma ni se resta nada.
 */
export function CapitalSection({ from, to }: { from: string; to: string }) {
  const [createOpen, setCreateOpen] = useState(false);
  const [toVoid, setToVoid] = useState<CapitalMovement | null>(null);

  const summary = useQuery({
    queryKey: ["capital-summary", from, to],
    queryFn: () => ownerCapitalService.getSummary({ from_date: from, to_date: to }),
    staleTime: 60 * 1000,
  });
  const movements = useQuery({
    queryKey: ["capital-movements", from, to],
    queryFn: () => ownerCapitalService.list({ from_date: from, to_date: to }),
    staleTime: 60 * 1000,
  });

  const data = summary.data;
  const showInKind = data?.has_in_kind ?? false;
  const inconsistent = data?.partners.some((p) => p.prestamo_pendiente_inconsistente) ?? false;

  return (
    <section aria-labelledby="capital-socios-title" className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2
            id="capital-socios-title"
            className="font-display text-lg font-semibold text-vk-text-primary"
          >
            Capital de los socios
          </h2>
          <p className="mt-1 max-w-2xl text-xs text-vk-text-secondary">
            Aportes, retiros y préstamos de los socios. No están en Ingresos, Egresos ni
            Resultado, que miden la operación del negocio. Tampoco son la caja contada: eso
            es el arqueo.
          </p>
        </div>
        <Button size="sm" variant="secondary" onClick={() => setCreateOpen(true)}>
          <Users className="h-4 w-4" />
          Registrar movimiento de socio
        </Button>
      </div>

      <CapitalMovementModal isOpen={createOpen} onClose={() => setCreateOpen(false)} />
      <VoidMovementModal movement={toVoid} onClose={() => setToVoid(null)} />

      {summary.isLoading ? (
        <div className="h-24 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w" />
      ) : summary.isError ? (
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          No se pudo cargar el capital de los socios.
        </p>
      ) : !data || !data.has_data ? (
        <p className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-sm text-vk-text-secondary">
          No hay aportes, retiros ni préstamos de socios en este período.
        </p>
      ) : (
        <>
          {data.cash_flow && (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
              <StatCard
                label="Entró de los socios"
                value={formatARS(data.cash_flow.entradas)}
                sublabel="en dinero"
              />
              <StatCard
                label="Salió hacia los socios"
                value={formatARS(data.cash_flow.salidas)}
                sublabel="en dinero"
              />
              <StatCard
                label="Flujo de capital"
                value={formatARS(data.cash_flow.neto)}
                sublabel="entradas menos salidas"
              />
            </div>
          )}

          <div className="overflow-x-auto rounded-lg border border-vk-border-w bg-vk-surface-w">
            <table className="w-full text-sm text-vk-text-primary">
              <thead className="text-xs text-vk-text-secondary">
                <tr>
                  <th scope="col" className="px-3 py-2 text-left font-medium">Socio</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Aportes</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Retiros</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Préstamos recibidos
                  </th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">Devoluciones</th>
                  <th scope="col" className="px-3 py-2 text-right font-medium">
                    Préstamo pendiente
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.partners.map((p) => (
                  <PartnerRow
                    key={p.partner_name ?? "__sin_socio__"}
                    row={p}
                    showInKind={showInKind}
                  />
                ))}
                {data.totals && (
                  <PartnerRow row={data.totals} showInKind={showInKind} isTotal />
                )}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-vk-text-muted">
            Los montos son del período elegido; el préstamo pendiente es lo acumulado hasta
            el último día del período.
          </p>
          {inconsistent && (
            <p className="rounded-lg border border-vk-warning/30 bg-vk-warning-bg/40 px-4 py-2 text-xs text-vk-text-secondary">
              Hay devoluciones mayores que lo prestado. Revisá si falta cargar un préstamo o si
              alguna devolución está de más.
            </p>
          )}
        </>
      )}

      {movements.data && movements.data.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-vk-border-w bg-vk-surface-w">
          <table className="w-full text-sm text-vk-text-primary">
            <caption className="px-3 pt-2 text-left text-xs font-medium text-vk-text-secondary">
              Movimientos del período
              {movements.data.length >= CAPITAL_LIST_LIMIT && (
                <span className="block font-normal">
                  Se muestran los {CAPITAL_LIST_LIMIT} más recientes; puede haber más. Los totales de
                  arriba incluyen todos los movimientos del período.
                </span>
              )}
            </caption>
            <thead className="text-xs text-vk-text-secondary">
              <tr>
                <th scope="col" className="px-3 py-2 text-left font-medium">Fecha</th>
                <th scope="col" className="px-3 py-2 text-left font-medium">Tipo</th>
                <th scope="col" className="px-3 py-2 text-left font-medium">Socio</th>
                <th scope="col" className="px-3 py-2 text-left font-medium">Medio</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Monto</th>
                <th scope="col" className="px-3 py-2 text-left font-medium">Nota</th>
                <th scope="col" className="px-3 py-2">
                  <span className="sr-only">Acciones</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {movements.data.map((m) => (
                <tr key={m.id} className="border-t border-vk-border-w">
                  <td className="px-3 py-2">{m.occurred_on}</td>
                  <td className="px-3 py-2">{CAPITAL_KIND_LABELS[m.kind] ?? m.kind}</td>
                  <td className="px-3 py-2">{m.partner_name ?? "—"}</td>
                  <td className="px-3 py-2">
                    {m.payment_method ? paymentLabel(m.payment_method) : "En especie"}
                  </td>
                  <td className="px-3 py-2 text-right">{formatARS(m.amount_ars)}</td>
                  <td className="px-3 py-2 text-vk-text-secondary">{m.notes ?? ""}</td>
                  <td className="px-3 py-2 text-right">
                    <Button
                      size="sm"
                      variant="secondary"
                      onClick={() => setToVoid(m)}
                      aria-label={`Anular ${CAPITAL_KIND_LABELS[m.kind] ?? m.kind} del ${m.occurred_on}`}
                    >
                      Anular
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
