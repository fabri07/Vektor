import type { InsightResponse } from "@/types/api";

interface Props {
  insight: InsightResponse;
}

const RISK_ICONS: Record<string, React.ReactNode> = {
  cash: (
    <svg className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 18.75a60.07 60.07 0 0115.797 2.101c.727.198 1.453-.342 1.453-1.096V18.75M3.75 4.5v.75A.75.75 0 013 6h-.75m0 0v-.375c0-.621.504-1.125 1.125-1.125H20.25M2.25 6v9m18-10.5v.75c0 .414.336.75.75.75h.75m-1.5-1.5h.375c.621 0 1.125.504 1.125 1.125v9.75c0 .621-.504 1.125-1.125 1.125h-.375m1.5-1.5H21a.75.75 0 00-.75.75v.75m0 0H3.75m0 0h-.375a1.125 1.125 0 01-1.125-1.125V15m1.5 1.5v-.75A.75.75 0 003 15h-.75" />
    </svg>
  ),
  margin: (
    <svg className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 6L9 12.75l4.286-4.286a11.948 11.948 0 014.306 6.43l.776 2.898m0 0l3.182-5.511m-3.182 5.51l-5.511-3.181" />
    </svg>
  ),
  stock: (
    <svg className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M20.25 7.5l-.625 10.632a2.25 2.25 0 01-2.247 2.118H6.622a2.25 2.25 0 01-2.247-2.118L3.75 7.5M10 11.25h4M3.375 7.5h17.25c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125H3.375c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z" />
    </svg>
  ),
  supplier: (
    <svg className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 18.75a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m3 0h6m-9 0H3.375a1.125 1.125 0 01-1.125-1.125V14.25m17.25 4.5a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m3 0h1.125c.621 0 1.129-.504 1.09-1.124a17.902 17.902 0 00-3.213-9.193 2.056 2.056 0 00-1.58-.86H14.25M16.5 18.75h-2.25m0-11.177v-.958c0-.568-.422-1.048-.987-1.106a48.554 48.554 0 00-10.026 0 1.106 1.106 0 00-.987 1.106v7.635m12-6.677v6.677m0 4.5v-4.5m0 0h-12" />
    </svg>
  ),
};

function resolveIcon(insightType: string, riskCode: string): React.ReactNode {
  const key = (riskCode || insightType || "").toLowerCase();
  if (key.includes("cash") || key.includes("caja")) return RISK_ICONS.cash;
  if (key.includes("margin") || key.includes("margen")) return RISK_ICONS.margin;
  if (key.includes("stock") || key.includes("inventario")) return RISK_ICONS.stock;
  if (key.includes("supplier") || key.includes("proveedor")) return RISK_ICONS.supplier;
  return RISK_ICONS.cash;
}

export function RiskCard({ insight }: Props) {
  const isCritical = insight.severity_code?.toUpperCase() === "CRITICAL";
  const icon = resolveIcon(insight.insight_type, insight.severity_code);

  return (
    <div
      className={`rounded-xl border bg-white p-6 shadow-sm ${
        isCritical ? "border-red-200" : "border-gray-200"
      }`}
    >
      <p className="mb-3 text-xs font-medium uppercase tracking-widest text-gray-400">
        Riesgo Principal
      </p>
      <div className="flex items-start gap-3">
        <div
          className={`mt-0.5 rounded-lg p-2 ${
            isCritical
              ? "bg-red-50 text-red-600"
              : "bg-gray-50 text-gray-500"
          }`}
        >
          {icon}
        </div>
        <div className="min-w-0">
          <p
            className={`text-sm font-semibold ${
              isCritical ? "text-red-700" : "text-gray-800"
            }`}
          >
            {insight.title}
          </p>
          <p className="mt-1 text-sm leading-relaxed text-gray-500">
            {insight.description}
          </p>
        </div>
      </div>
    </div>
  );
}
