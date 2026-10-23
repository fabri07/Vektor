"use client";

import { AlertTriangle } from "lucide-react";

import type { RereadDataLoss } from "@/services/ingestion.service";

/**
 * Compuerta de relectura — el apply midió que se pierde algo y NO guardó nada.
 * Aplicar igual es una decisión explícita. Controlado: el botón vive en el modal,
 * que es quien sabe si la tilde está puesta.
 */
export function RereadPerdidasAviso({
  perdida,
  acepto,
  onAceptoChange,
}: {
  perdida: RereadDataLoss;
  acepto: boolean;
  onAceptoChange: (acepto: boolean) => void;
}) {
  const perdidos = perdida.to_lose;
  const aOtros = perdida.to_lose_to_others;
  const enRiesgo = perdida.edited_at_risk;
  return (
    <div
      role="alert"
      className="rounded-lg border border-vk-danger/40 bg-vk-danger-bg/40 px-3 py-2.5"
    >
      <p className="flex gap-1.5 text-xs font-semibold text-vk-danger">
        <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
        <span>La relectura no se aplicó: pierde datos</span>
      </p>
      <p className="mt-1 text-xs leading-snug text-vk-text-primary">
        Lo medimos aplicándola, y no se guardó nada. Esto es lo que pasaría:
      </p>
      <ul className="mt-1 list-disc pl-5 text-xs leading-snug text-vk-text-primary">
        {perdidos > 0 && (
          <li>
            {perdidos} registro(s) no vuelven como venta o gasto
            {aOtros > 0 && (
              <>
                {" "}— {aOtros} pasan a «Otros», porque la lectura nueva no los
                toma como ventas ni gastos
              </>
            )}
            . Los ves abajo, marcados «Se pierde».
          </li>
        )}
        {enRiesgo > 0 && (
          <li>
            {enRiesgo} edición(es) a mano quedarían repetidas:{" "}
            {perdida.motivo_texto ??
              "las filas del archivo se corrieron de lugar desde que las corregiste."}
          </li>
        )}
      </ul>
      {enRiesgo > 0 && (
        // `accept_data_loss` NO descarta la edición: la conserva y la fila
        // original vuelve a entrar. No se promete una salida que no existe.
        <p className="mt-1 text-xs leading-snug text-vk-text-primary">
          Aplicar igual no borra tu edición: queda junto a la fila original,
          repetida. Si no querés eso, dejá el archivo como está y revisá el
          conflicto.
        </p>
      )}
      <label className="mt-2 flex items-start gap-2 text-xs text-vk-text-primary">
        <input
          type="checkbox"
          checked={acepto}
          onChange={(e) => onAceptoChange(e.target.checked)}
          className="mt-0.5"
        />
        <span>Revisé lo que se pierde y quiero aplicar la relectura igual.</span>
      </label>
    </div>
  );
}
