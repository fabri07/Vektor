"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { AlertTriangle, TrendingDown, Package, Users, X, MessageCircle } from "lucide-react";
import type { HealthScoreV2Response } from "@/types/api";
import { useChatWidgetStore } from "@/stores/chatWidgetStore";

interface AlertConfig {
  icon: React.ReactNode;
  title: string;
  body: string;
  href: string;
  colorClass: string;
  /** Pregunta que se manda al chat con "Preguntarle a Véktor". */
  chatPrompt: string;
}

const ALERT_MAP: Record<string, AlertConfig> = {
  CASH_LOW: {
    icon: <TrendingDown className="h-4 w-4" />,
    title: "Caja en riesgo",
    body: "Tu cobertura de caja está por debajo del umbral saludable. Revisá tus egresos.",
    href: "/dashboard",
    colorClass: "border-vk-danger/60 bg-vk-danger/10 text-vk-danger",
    chatPrompt: "Explicame por qué está en rojo la caja",
  },
  MARGIN_LOW: {
    icon: <AlertTriangle className="h-4 w-4" />,
    title: "Margen bajo",
    body: "Tu rentabilidad está por debajo del rango esperado para tu rubro.",
    href: "/dashboard/analisis",
    colorClass: "border-amber-500/60 bg-amber-500/10 text-amber-400",
    chatPrompt: "Explicame por qué está en rojo el margen",
  },
  STOCK_CRITICAL: {
    icon: <Package className="h-4 w-4" />,
    title: "Stock crítico",
    body: "Más del 30% de tus productos están por debajo del umbral mínimo.",
    href: "/products?stock=low",
    colorClass: "border-orange-500/60 bg-orange-500/10 text-orange-400",
    chatPrompt: "Explicame por qué está en rojo el stock",
  },
  SUPPLIER_DEPENDENCY: {
    icon: <Users className="h-4 w-4" />,
    title: "Alta dependencia de proveedor",
    body: "Concentrás casi toda tu operación en un solo proveedor.",
    href: "/dashboard/analisis",
    colorClass: "border-yellow-500/60 bg-yellow-500/10 text-yellow-400",
    chatPrompt: "Explicame esta alerta de dependencia de proveedor",
  },
};

interface Props {
  score: HealthScoreV2Response;
}

export function HealthAlertBanner({ score }: Props) {
  const router = useRouter();
  const openWithPrompt = useChatWidgetStore((s) => s.openWithPrompt);
  const [visible, setVisible] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  const config = ALERT_MAP[score.primary_risk_code];
  const shouldShow = config != null && score.score_total < 75 && !dismissed;

  useEffect(() => {
    if (!shouldShow) return;
    const timer = setTimeout(() => setVisible(true), 800);
    return () => clearTimeout(timer);
  }, [shouldShow]);

  if (!shouldShow || !visible) return null;

  return (
    <div
      className={`fixed bottom-6 right-6 z-50 w-72 rounded-xl border shadow-lg transition-all duration-300 ${config.colorClass} ${
        visible ? "translate-y-0 opacity-100" : "translate-y-4 opacity-0"
      }`}
    >
      <div className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-start gap-2.5">
            <span className="mt-0.5 shrink-0">{config.icon}</span>
            <div>
              <p className="text-sm font-semibold leading-snug">{config.title}</p>
              <p className="mt-1 text-xs leading-snug opacity-80">{config.body}</p>
            </div>
          </div>
          <button
            onClick={() => setDismissed(true)}
            className="shrink-0 opacity-60 hover:opacity-100 transition-opacity"
            aria-label="Cerrar"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="mt-3 flex gap-2">
          <button
            onClick={() => {
              setDismissed(true);
              router.push(config.href);
            }}
            className="flex-1 rounded-lg border border-current/30 py-1.5 text-xs font-medium opacity-90 hover:opacity-100 transition-opacity"
          >
            Ver en el dashboard →
          </button>
          <button
            onClick={() => {
              setDismissed(true);
              openWithPrompt({
                message: config.chatPrompt,
                uiContext: {
                  view: "dashboard",
                  active_alert_ids: [score.primary_risk_code],
                },
              });
            }}
            className="flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-current/30 py-1.5 text-xs font-medium opacity-90 hover:opacity-100 transition-opacity"
          >
            <MessageCircle className="h-3.5 w-3.5" />
            Preguntarle a Véktor
          </button>
        </div>
      </div>
    </div>
  );
}
