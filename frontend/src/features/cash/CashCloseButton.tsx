"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Lock } from "lucide-react";
import { cashCloseService } from "@/services/cashClose.service";
import { CashCloseModal } from "@/features/cash/CashCloseModal";

function todayISO(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/**
 * Botón de cierre de caja. Siempre visible. Se destaca (color de alerta + badge)
 * cuando el preview indica `is_past_close_now` (pasó la hora de cierre del día
 * laboral en ART y no hay cierre registrado hoy).
 */
export function CashCloseButton() {
  const [open, setOpen] = useState(false);
  const closeDate = todayISO();

  const { data: preview } = useQuery({
    queryKey: ["cash-closes-today", closeDate],
    queryFn: () => cashCloseService.getPreview(closeDate),
    staleTime: 5 * 60 * 1000,
  });

  const alreadyClosed = preview?.already_closed ?? false;
  const highlight = preview?.is_past_close_now ?? false;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={[
          "inline-flex items-center gap-2 rounded-full px-4 py-1.5 text-sm font-medium transition-colors",
          alreadyClosed
            ? "border border-vk-border-w bg-vk-surface-w text-vk-text-muted"
            : highlight
              ? "bg-vk-danger text-white animate-pulse"
              : "border border-vk-border-w bg-vk-surface-w text-vk-text-secondary hover:text-vk-text-primary",
        ].join(" ")}
        title={
          alreadyClosed
            ? "Caja cerrada hoy"
            : highlight
              ? "Pendiente: cerrá la caja del día"
              : "Cierre de caja"
        }
      >
        <Lock className="h-3.5 w-3.5" />
        {alreadyClosed
          ? "Caja cerrada"
          : highlight
            ? "Cerrar caja (pendiente)"
            : "Cierre de caja"}
      </button>

      <CashCloseModal isOpen={open} onClose={() => setOpen(false)} />
    </>
  );
}
