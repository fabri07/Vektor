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
  /**
   * El catálogo guardado en la PC. Sin red, la búsqueda por nombre cae acá: si
   * no, sin conexión no se podía ni buscar un producto sin código ni aprender
   * uno nuevo.
   */
  local?: readonly PosProduct[];
}

/** Búsqueda local por nombre o SKU, sin acentos ni mayúsculas. */
export function buscarLocal(productos: readonly PosProduct[], termino: string): PosProduct[] {
  const plano = (t: string) =>
    t.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
  const q = plano(termino.trim());
  if (q.length < 2) return [];
  return productos
    .filter((p) => plano(p.name).includes(q) || plano(p.sku ?? "").includes(q))
    .slice(0, 20);
}

const TITLES: Record<PickerMode["mode"], string> = {
  search: "Buscar producto",
  ambiguous: "¿Cuál de estos productos es?",
  learn: "Código nuevo: ¿a qué producto corresponde?",
};

export function ProductPicker({ picker, onPick, onClose, local }: ProductPickerProps) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<PosProduct[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sinRed, setSinRed] = useState(false);
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
            setSinRed(false);
          }
        })
        .catch(() => {
          if (!vigente) return;
          if (local && local.length > 0) {
            setResults(buscarLocal(local, term));
            setError(null);
            setSinRed(true);
          } else {
            setError("No se pudo buscar. Revisá la conexión.");
          }
        });
    }, 250);
    return () => {
      vigente = false;
      clearTimeout(t);
    };
  }, [q, buscar, local]);

  const lista = picker.mode === "ambiguous" ? picker.candidates : results;

  return (
    <Modal isOpen onClose={onClose} title={TITLES[picker.mode]} size="lg">
      {picker.mode !== "search" ? (
        <p className="mb-3 text-sm text-vektor-body">
          Código leído: <span className="font-mono">{picker.code}</span>
          {picker.mode === "learn" ? " — queda vinculado al producto que elijas." : null}
        </p>
      ) : null}
      {sinRed ? (
        <p className="mb-2 text-xs text-vektor-muted">
          Sin conexión: buscando en el catálogo guardado en esta PC.
        </p>
      ) : null}
      {buscar ? (
        <input
          autoFocus
          aria-label="Buscar por nombre"
          placeholder="Nombre del producto"
          className="mb-3 h-10 w-full rounded-lg border border-vektor-border bg-vektor-surface px-3 text-sm text-vektor-white focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
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
              className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm hover:bg-vektor-surface"
              onClick={() => onPick(p)}
            >
              <span className="text-vektor-white">{p.name}</span>
              <span className="text-vektor-body">
                {formatCents(toCents(p.sale_price_ars) ?? 0)}
              </span>
            </button>
          </li>
        ))}
        {buscar && q.trim().length >= 2 && lista.length === 0 && !error ? (
          <li className="px-3 py-2 text-sm text-vektor-muted">Sin resultados.</li>
        ) : null}
      </ul>
    </Modal>
  );
}
