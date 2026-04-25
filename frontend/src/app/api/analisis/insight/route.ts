import { NextRequest, NextResponse } from "next/server";

const INSIGHTS: Record<string, string> = {
  caja:
    "Tu caja se mantuvo estable, pero los picos de egresos de mitad de período te dejan con menos margen para reaccionar ante imprevistos. Si sostenés este ritmo, conviene reforzar liquidez antes del próximo cierre semanal.",
  ventas:
    "Las ventas muestran una tendencia positiva, aunque todavía dependen de pocos momentos del mes. Hay oportunidad para repartir mejor el ingreso y evitar semanas demasiado flojas.",
  margen:
    "El margen promedio viene sosteniéndose, pero algunas categorías siguen por debajo del umbral saludable. Priorizar precio, mix y reposición en esos rubros puede mejorar la rentabilidad total.",
  stock:
    "El stock total luce razonable, pero la distribución no es pareja: hay categorías con sobrestock y otras que ya están cerca del quiebre. Vale la pena revisar reposición por estado y rotación.",
  default:
    "La evolución general del negocio es estable, pero todavía hay señales mixtas entre liquidez, rotación y rentabilidad. Mirar estas métricas juntas te da una lectura más clara de qué corregir primero.",
};

export function GET(request: NextRequest) {
  const metric = request.nextUrl.searchParams.get("metric") ?? "default";
  const period = request.nextUrl.searchParams.get("period") ?? "30d";

  return NextResponse.json({
    metric,
    period,
    insight: INSIGHTS[metric] ?? INSIGHTS.default,
    // TODO: wire to LLM layer with tenant-aware context and selected filter state.
  });
}
