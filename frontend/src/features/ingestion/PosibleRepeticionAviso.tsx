"use client";

import { AlertTriangle } from "lucide-react";
import { useState } from "react";

/**
 * Fase 2 — el confirm se frenó porque el archivo repite, en su mayoría, lo que
 * ya estaba cargado. No se guardó nada. Para importarlo igual la persona tiene
 * que marcar explícitamente que lo sabe: sin clave fuerte, coincidir en fecha e
 * importe no prueba que sea un duplicado, así que la decisión es suya.
 */
export function PosibleRepeticionAviso({
  mensaje,
  enviando,
  onImportarIgual,
}: {
  mensaje: string;
  enviando: boolean;
  onImportarIgual: () => void;
}) {
  const [acepto, setAcepto] = useState(false);
  return (
    <div
      role="alert"
      className="mt-3 rounded-lg border border-vk-warning/40 bg-vk-warning-bg/40 px-3 py-2.5"
    >
      <p className="flex gap-1.5 text-xs font-semibold text-vk-warning">
        <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
        <span>Este archivo parece una carga repetida</span>
      </p>
      <p className="mt-1 text-xs leading-snug text-vk-text-primary">{mensaje}</p>
      <label className="mt-2 flex items-start gap-2 text-xs text-vk-text-primary">
        <input
          type="checkbox"
          checked={acepto}
          onChange={(e) => setAcepto(e.target.checked)}
          className="mt-0.5"
        />
        <span>Sé que puede repetir operaciones ya cargadas y quiero importarlo igual.</span>
      </label>
      <button
        type="button"
        onClick={onImportarIgual}
        disabled={!acepto || enviando}
        className="mt-2 rounded-lg bg-vk-blue px-3 py-1.5 text-xs font-medium text-white hover:bg-vk-blue-hover disabled:opacity-50 transition-colors"
      >
        {enviando ? "Importando..." : "Importar igual"}
      </button>
    </div>
  );
}
