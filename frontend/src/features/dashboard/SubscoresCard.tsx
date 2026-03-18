import type { HealthScoreV2Response } from "@/types/api";

interface Props {
  score: HealthScoreV2Response;
}

function scoreColorClass(value: number): string {
  if (value > 70) return "bg-vk-success";
  if (value >= 40) return "bg-vk-warning";
  return "bg-vk-danger";
}

export function SubscoresCard({ score }: Props) {
  const subscores = [
    { label: "Caja", value: score.score_cash },
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
              <span className="text-sm font-medium text-vk-text-secondary">{sub.label}</span>
              <span className="text-sm font-semibold text-vk-text-primary">{sub.value}</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-vk-border-w">
              <div
                className={`h-full rounded-full transition-all ${scoreColorClass(sub.value)}`}
                style={{ width: `${sub.value}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
