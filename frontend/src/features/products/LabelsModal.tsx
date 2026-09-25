"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { type LabelFormat, type LabelProduct, renderLabels } from "@/features/products/labelSheet";
import { createBrowserPrinter } from "@/lib/print/browserPrinter";
import { posService } from "@/services/pos.service";
import { usePosPrintConfigStore } from "@/stores/posPrintConfigStore";

const printer = createBrowserPrinter();

/**
 * Imprimir etiquetas con el código interno (B12). Busca con el catálogo de
 * caja (`/pos/catalog`): trae el `internal_sku` y el precio, sin costos.
 */
export function LabelsModal({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const { paperWidth, pageSize } = usePosPrintConfigStore();
  const [q, setQ] = useState("");
  const [results, setResults] = useState<LabelProduct[]>([]);
  const [elegidos, setElegidos] = useState<{ product: LabelProduct; copies: number }[]>([]);
  const [format, setFormat] = useState<LabelFormat>("a4");
  const [showPrice, setShowPrice] = useState(true);
  const [aviso, setAviso] = useState<string | null>(null);

  useEffect(() => {
    const term = q.trim();
    if (term.length < 2) {
      setResults([]);
      return;
    }
    let vigente = true;
    const t = setTimeout(() => {
      posService
        .searchCatalog(term)
        .then((r) => vigente && setResults(r))
        .catch(() => vigente && setAviso("No se pudo buscar. Revisá la conexión."));
    }, 250);
    return () => {
      vigente = false;
      clearTimeout(t);
    };
  }, [q]);

  function agregar(p: LabelProduct): void {
    setElegidos((prev) =>
      prev.some((x) => x.product.id === p.id) ? prev : [...prev, { product: p, copies: 1 }],
    );
  }

  async function imprimir(): Promise<void> {
    const { doc, skipped, count } = renderLabels(elegidos, {
      format,
      showPrice,
      paperWidth,
      pageSize,
    });
    const faltan = skipped.length
      ? ` Sin código interno (no se imprimen): ${skipped.join(", ")}.`
      : "";
    if (count === 0) {
      setAviso(`No hay etiquetas para imprimir.${faltan}`);
      return;
    }
    try {
      await printer.print(doc);
      setAviso(`Se mandaron ${count} etiqueta(s) a imprimir.${faltan}`);
    } catch {
      setAviso("No se pudo imprimir. Revisá la impresora y probá de nuevo.");
    }
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Imprimir etiquetas" size="xl">
      <div className="space-y-4 text-sm">
        <input
          aria-label="Buscar producto para etiquetar"
          placeholder="Buscá un producto por nombre"
          className="h-10 w-full rounded-lg border border-vk-border-w bg-vk-surface-w px-3 text-vk-text-primary"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        {results.length ? (
          <ul className="max-h-40 overflow-auto rounded-lg border border-vk-border-w">
            {results.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  className="w-full px-3 py-1.5 text-left hover:bg-vk-bg-light"
                  onClick={() => agregar(p)}
                >
                  {p.name}
                  {p.internal_sku ? (
                    <span className="ml-2 font-mono text-xs text-vk-text-muted">{p.internal_sku}</span>
                  ) : (
                    <span className="ml-2 text-xs text-vk-warning">sin código interno</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        ) : null}

        {elegidos.length ? (
          <ul className="space-y-1">
            {elegidos.map(({ product, copies }) => (
              <li key={product.id} className="flex items-center justify-between gap-2">
                <span className="text-vk-text-primary">{product.name}</span>
                <span className="flex items-center gap-2">
                  <input
                    aria-label={`Copias de ${product.name}`}
                    type="number"
                    min={1}
                    max={500}
                    className="h-8 w-20 rounded border border-vk-border-w bg-vk-surface-w px-2 text-right"
                    value={copies}
                    onChange={(e) => {
                      const n = Math.max(1, Math.min(500, Number(e.target.value) || 1));
                      setElegidos((prev) =>
                        prev.map((x) => (x.product.id === product.id ? { ...x, copies: n } : x)),
                      );
                    }}
                  />
                  <button
                    type="button"
                    className="text-vk-text-muted hover:text-vk-danger"
                    onClick={() => setElegidos((prev) => prev.filter((x) => x.product.id !== product.id))}
                  >
                    Quitar
                  </button>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-vk-text-muted">Elegí los productos a etiquetar.</p>
        )}

        <div className="flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-2">
            Formato
            <select
              aria-label="Formato de etiqueta"
              className="rounded border border-vk-border-w bg-vk-surface-w px-2 py-1"
              value={format}
              onChange={(e) => setFormat(e.target.value as LabelFormat)}
            >
              <option value="a4">Hoja A4 (3 × 8)</option>
              <option value="rollo">Rollo de la térmica ({paperWidth} mm)</option>
            </select>
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={showPrice} onChange={(e) => setShowPrice(e.target.checked)} />
            Mostrar precio
          </label>
        </div>

        {aviso ? <p role="status" className="text-vk-text-secondary">{aviso}</p> : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cerrar
          </Button>
          <Button disabled={elegidos.length === 0} onClick={() => void imprimir()}>
            Imprimir
          </Button>
        </div>
      </div>
    </Modal>
  );
}
