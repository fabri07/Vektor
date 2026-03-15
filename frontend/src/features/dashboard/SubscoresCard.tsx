import type { HealthScoreV2Response } from "@/types/api";

interface Props {
  score: HealthScoreV2Response;
}

interface SubScore {
  label: string;
  value: number;
  color: string;
}

function scoreColor(value: number): string {
  if (value >= 70) return "bg-emerald-500";
  if (value >= 45) return "bg-amber-400";
  return "bg-red-500";
}

export function SubscoresCard({ score }: Props) {
  const subscores: SubScore[] = [
    { label: "Caja", value: score.score_cash, color: scoreColor(score.score_cash) },
    { label: "Margen", value: score.score_margin, color: scoreColor(score.score_margin) },
    { label: "Stock", value: score.score_stock, color: scoreColor(score.score_stock) },
    { label: "Proveedores", value: score.score_supplier, color: scoreColor(score.score_supplier) },
  ];

  return (
    <div className="col-span-2 rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
      <p className="mb-5 text-xs font-medium uppercase tracking-widest text-gray-400">
        Dimensiones
      </p>
      <div className="space-y-4">
        {subscores.map((sub) => (
          <div key={sub.label}>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-sm font-medium text-gray-700">{sub.label}</span>
              <span className="text-sm font-semibold text-gray-800">{sub.value}</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-gray-100">
              <div
                className={`h-full rounded-full transition-all ${sub.color}`}
                style={{ width: `${sub.value}%` }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
