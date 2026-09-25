"use client";

import { useEffect, useState } from "react";

import { Modal } from "@/components/ui/Modal";
import { type PosProduct, formatCents, toCents } from "@/lib/pos/cart";
import { posService } from "@/services/pos.service";

/**
 * Elegir un producto a mano. Tres usos:
 * - `search`: no tiene código, se busca por nombre.
 * - `ambiguous`: el código responde a más de un producto (`SCAN_AMBIGUOUS`). Se
 *   muestran SÓLO los candidatos: cobrar el equivocado es peor que preguntar.
 * - `learn`: el código es desconocido y se puede aprender. Se busca el producto
 *   y, al elegirlo, se le vincula el código.
 *
 * El buscador es texto libre: el lector está apagado mientras el modal está
 * abierto (lo decide `PosScreen`).
 */
export type PickerMode =
  | { mode: "search" }
  | { mode: "ambiguous"; code: string; candidates: PosProduct[] }
  | { mode: "learn"; code: string };

interface ProductPickerProps {
  picker: PickerMode;
  onPick: (product: PosProduct) => void;
  onClose: () => void;
}

const TITLES: Record<PickerMode["mode"], string> = {
  search: "Buscar producto",
  ambiguous: "¿Cuál de estos productos es?",
  learn: "Código nuevo: ¿a qué producto corresponde?",
};

export function ProductPicker({ picker, onPick, onClose }: ProductPickerProps) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<PosProduct[]>([]);
  const [error, setError] = useState<string | null>(null);
  const buscar = picker.mode !== "ambiguous";

  useEffect(() => {
    if (!buscar) return;
    const term = q.trim();
    if (term.length < 2) {
      setResults([]);
      return;
    }
    let vigente = true;
    const t = setTimeout(() => {
      posService
        .searchCatalog(term)
        .then((items) => {
          if (vigente) {
            setResults(items);
            setError(null);
          }
        })
        .catch(() => {
          if (vigente) setError("No se pudo buscar. Revisá la conexión.");
        });
    }, 250);
    return () => {
      vigente = false;
      clearTimeout(t);
    };
  }, [q, buscar]);

  const lista = picker.mode === "ambiguous" ? picker.candidates : results;

  return (
    <Modal isOpen onClose={onClose} title={TITLES[picker.mode]} size="lg">
      {picker.mode !== "search" ? (
        <p className="mb-3 text-sm text-vk-text-secondary">
          Código leído: <span className="font-mono">{picker.code}</span>
          {picker.mode === "learn" ? " — queda vinculado al producto que elijas." : null}
        </p>
      ) : null}
      {buscar ? (
        <input
          autoFocus
          aria-label="Buscar por nombre"
          placeholder="Nombre del producto"
          className="mb-3 h-10 w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-3 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      ) : null}
      {error ? <p className="mb-2 text-sm text-vk-danger">{error}</p> : null}
      <ul className="max-h-80 space-y-1 overflow-auto">
        {lista.map((p) => (
          <li key={p.id}>
            <button
              type="button"
              className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm hover:bg-vk-bg-light"
              onClick={() => onPick(p)}
            >
              <span className="text-vk-text-primary">{p.name}</span>
              <span className="text-vk-text-secondary">
                {formatCents(toCents(p.sale_price_ars) ?? 0)}
              </span>
            </button>
          </li>
        ))}
        {buscar && q.trim().length >= 2 && lista.length === 0 && !error ? (
          <li className="px-3 py-2 text-sm text-vk-text-muted">Sin resultados.</li>
        ) : null}
      </ul>
    </Modal>
  );
}
