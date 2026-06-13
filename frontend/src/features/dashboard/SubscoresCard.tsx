import type { HealthScoreV2Response } from "@/types/api";

interface Props {
  score: HealthScoreV2Response;
}

function scoreColorClass(value: number): string {
  if (value > 70) return "bg-vk-success";
  if (value >= 40) return "bg-vk-warning";
  return "bg-vk-danger";
}

// Nota explicativa de la dimensión Caja según la fuente del dato.
const CASH_SOURCE_NOTE: Record<string, string> = {
  arqueo: "Saldo medido (arqueo)",
  onboarding: "Estimada (onboarding)",
  flujo: "Estimada por movimientos",
};

export function SubscoresCard({ score }: Props) {
  const cashUnknown = score.cash_source === "desconocido";
  const cashNote = score.cash_source ? CASH_SOURCE_NOTE[score.cash_source] : undefined;
  const showArqueoReminder = score.cash_source === "flujo";

  const subscores = [
    { label: "Caja", value: score.score_cash, note: cashNote, unknown: cashUnknown },
    { label: "Margen", value: score.score_margin },
    { label: "Stock", value: score.score_stock },
    { label: "Proveedores", value: score.score_supplier },
  ];

  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm transition-[box-shadow,transform] duration-200 hover:shadow-vk-md hover:-translate-y-0.5">
      <p className="mb-5 text-xs font-medium uppercase tracking-widest text-vk-text-muted">
        Dimensiones
      </p>
      <div className="space-y-4">
        {subscores.map((sub) => (
          <div key={sub.label}>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-sm font-medium text-vk-text-secondary">
                {sub.label}
                {sub.note && (
                  <span className="ml-2 text-xs font-normal text-vk-text-muted">{sub.note}</span>
                )}
              </span>
              {sub.unknown ? (
                <span className="text-xs font-medium text-vk-text-muted">Sin datos</span>
              ) : (
                <span className="text-sm font-semibold text-vk-text-primary">{sub.value}</span>
              )}
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-vk-border-w">
              {!sub.unknown && (
                <div
                  className={`h-full rounded-full transition-all ${scoreColorClass(sub.value)}`}
                  style={{ width: `${sub.value}%` }}
                />
              )}
            </div>
          </div>
        ))}
      </div>

      {showArqueoReminder && (
        <div className="mt-5 rounded-md border border-vk-warning/40 bg-vk-warning/10 p-3">
          <p className="text-xs leading-relaxed text-vk-text-secondary">
            <span className="font-semibold text-vk-text-primary">Caja estimada por movimientos.</span>{" "}
            Para un saldo real y confiable, hacé un{" "}
            <span className="font-medium">arqueo de caja</span> (cierre). Así el score de caja
            pasa a medirse, no estimarse.
          </p>
        </div>
      )}
    </div>
  );
}
