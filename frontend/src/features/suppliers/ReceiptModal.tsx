"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { AlertTriangle, FileUp, Loader2, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Modal } from "@/components/ui/Modal";
import {
  suppliersService,
  type ReceiptExtraction,
  type ReceiptLinePayload,
  type SupplierResponse,
} from "@/services/suppliers.service";
import { UploadSizeHint } from "@/components/ui/UploadSizeHint";
import { MAX_UPLOAD_BYTES, MAX_UPLOAD_MB } from "@/constants/upload";
import { useToastStore } from "@/stores/toastStore";

// Tipos que Vektor sabe leer para un remito (foto/PDF → IA; planilla → parser).
const ALLOWED_RECEIPT_EXTENSIONS =
  ".pdf,.xlsx,.xls,.csv,.png,.jpg,.jpeg,.webp,.heic,.heif";
const MAX_RECEIPT_BYTES = MAX_UPLOAD_BYTES; // 16 MB (alineado con el backend)

const CONFIDENCE_LABELS: Record<ReceiptExtraction["confidence"], string> = {
  HIGH: "Confianza alta",
  MEDIUM: "Confianza media",
  LOW: "Confianza baja",
};

interface ReceiptLineForm {
  product_name: string;
  sku: string;
  qty: string;
  unit_price: string;
}

function emptyReceiptLine(): ReceiptLineForm {
  return { product_name: "", sku: "", qty: "", unit_price: "" };
}

function lineFromExtraction(line: ReceiptExtraction["lines"][number]): ReceiptLineForm {
  return {
    product_name: line.product_name ?? "",
    sku: line.sku ?? "",
    qty: line.qty > 0 ? String(line.qty) : "",
    unit_price: line.unit_price >= 0 ? String(line.unit_price) : "",
  };
}

export function ReceiptModal({
  supplier,
  isOpen,
  onClose,
  onUploaded,
}: {
  supplier: SupplierResponse;
  isOpen: boolean;
  onClose: () => void;
  onUploaded: () => void | Promise<void>;
}) {
  const toast = useToastStore((s) => s.add);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [lines, setLines] = useState<ReceiptLineForm[]>(() => [emptyReceiptLine()]);
  const [shipping, setShipping] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [extraction, setExtraction] = useState<ReceiptExtraction | null>(null);

  useEffect(() => {
    if (isOpen) {
      setLines([emptyReceiptLine()]);
      setShipping("");
      setError(null);
      setExtraction(null);
    }
  }, [isOpen]);

  const idempotencyKey = useMemo(
    () => (typeof crypto !== "undefined" ? crypto.randomUUID() : `${Date.now()}`),
    // Regenerate per open so retries after a failure get a fresh key.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [isOpen],
  );

  const extractMutation = useMutation({
    mutationFn: (file: File) => suppliersService.extractReceipt(supplier.id, file),
    onSuccess: (result) => {
      setExtraction(result);
      // La IA solo prellena: las líneas quedan editables. Si no extrajo nada,
      // dejamos una fila vacía para no romper la edición manual.
      setLines(
        result.lines.length > 0
          ? result.lines.map(lineFromExtraction)
          : [emptyReceiptLine()],
      );
      setShipping(
        result.shipping_cost != null && result.shipping_cost >= 0
          ? String(result.shipping_cost)
          : "",
      );
      setError(null);
    },
    onError: () => toast("No se pudo leer el remito. Cargá las líneas a mano.", "error"),
  });

  const uploadMutation = useMutation({
    mutationFn: (payload: {
      lines: ReceiptLinePayload[];
      shipping_cost?: number;
      source_upload_id?: string;
    }) =>
      suppliersService.uploadReceipt(
        supplier.id,
        { ...payload, currency: "ARS" },
        idempotencyKey,
      ),
    onSuccess: () => {
      void onUploaded();
    },
    onError: () => toast("No se pudo cargar el remito.", "error"),
  });

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    // Reset para poder volver a elegir el mismo archivo tras un fallo.
    e.target.value = "";
    if (!file) return;
    if (file.size > MAX_RECEIPT_BYTES) {
      setError(`El archivo supera el límite de ${MAX_UPLOAD_MB} MB.`);
      return;
    }
    setError(null);
    extractMutation.mutate(file);
  }

  function setLine(index: number, key: keyof ReceiptLineForm, value: string) {
    setLines((prev) =>
      prev.map((line, i) => (i === index ? { ...line, [key]: value } : line)),
    );
    if (error) setError(null);
  }

  function addLine() {
    setLines((prev) => [...prev, emptyReceiptLine()]);
  }

  function removeLine(index: number) {
    setLines((prev) => (prev.length > 1 ? prev.filter((_, i) => i !== index) : prev));
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();

    const parsed: ReceiptLinePayload[] = [];
    for (const line of lines) {
      const name = line.product_name.trim();
      if (!name) {
        setError("Cada línea necesita un producto.");
        return;
      }
      const qty = Number(line.qty);
      if (!Number.isFinite(qty) || qty <= 0) {
        setError(`Cantidad inválida en "${name}". Debe ser mayor a 0.`);
        return;
      }
      if (!Number.isInteger(qty)) {
        // El backend trabaja con unidades enteras (stock/COGS) y rechaza fraccionarios.
        setError(`La cantidad en "${name}" debe ser un número entero de unidades.`);
        return;
      }
      const unitPrice = Number(line.unit_price);
      if (!Number.isFinite(unitPrice) || unitPrice < 0) {
        setError(`Precio unitario inválido en "${name}".`);
        return;
      }
      const sku = line.sku.trim();
      parsed.push({
        product_name: name,
        qty,
        unit_price: unitPrice,
        ...(sku ? { sku } : {}),
      });
    }

    if (parsed.length === 0) {
      setError("Agregá al menos una línea.");
      return;
    }

    let shippingCost: number | undefined;
    if (shipping.trim()) {
      const value = Number(shipping);
      if (!Number.isFinite(value) || value < 0) {
        setError("El costo de envío no puede ser negativo.");
        return;
      }
      shippingCost = value;
    }

    const sourceUploadId = extraction?.source_upload_id ?? undefined;
    uploadMutation.mutate({
      lines: parsed,
      ...(shippingCost !== undefined ? { shipping_cost: shippingCost } : {}),
      ...(sourceUploadId ? { source_upload_id: sourceUploadId } : {}),
    });
  }

  const extracting = extractMutation.isPending;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={`Cargar remito — ${supplier.name}`}
      size="2xl"
    >
      <form className="space-y-4" onSubmit={handleSubmit}>
        <p className="text-xs text-vk-text-muted">
          Los montos se registran en pesos argentinos (ARS).
        </p>

        {/* Subir archivo del remito (foto / PDF / planilla) → prellena las líneas */}
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="text-sm font-medium text-vk-text-primary">
                ¿Tenés el remito en foto, PDF o planilla?
              </p>
              <p className="text-xs text-vk-text-muted">
                Lo leemos y te prellenamos las líneas. Después revisás y editás.
              </p>
              <UploadSizeHint className="mt-1" />
            </div>
            <input
              ref={fileInputRef}
              type="file"
              accept={ALLOWED_RECEIPT_EXTENSIONS}
              className="hidden"
              onChange={handleFileChange}
              disabled={extracting || uploadMutation.isPending}
            />
            <Button
              type="button"
              variant="secondary"
              size="sm"
              loading={extracting}
              disabled={uploadMutation.isPending}
              onClick={() => fileInputRef.current?.click()}
            >
              {!extracting && <FileUp className="h-4 w-4" />}
              {extracting ? "Leyendo remito…" : "Subir remito (foto, PDF o planilla)"}
            </Button>
          </div>

          {extraction ? (
            <div
              className={`mt-3 flex items-start gap-2 rounded-lg border px-3 py-2 text-xs ${
                extraction.confidence === "HIGH"
                  ? "border-vk-success/20 bg-vk-success-bg text-vk-success"
                  : "border-vk-warning/20 bg-vk-warning-bg text-vk-warning"
              }`}
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <div className="space-y-1">
                <p className="font-medium">
                  {CONFIDENCE_LABELS[extraction.confidence]} · revisá las líneas antes de
                  confirmar.
                </p>
                {extraction.warnings.length > 0 && (
                  <ul className="list-disc space-y-0.5 pl-4">
                    {extraction.warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          ) : null}
        </div>

        <div className="space-y-2">
          <div className="grid grid-cols-[1fr_0.8fr_0.6fr_0.7fr_auto] gap-2 px-1 text-xs font-semibold text-vk-text-secondary">
            <span>Producto</span>
            <span>SKU (opcional)</span>
            <span>Cantidad</span>
            <span>Precio unit.</span>
            <span className="sr-only">Quitar</span>
          </div>

          {extracting ? (
            <div className="flex items-center gap-2 rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-6 text-sm text-vk-text-muted">
              <Loader2 className="h-4 w-4 animate-spin" />
              Leyendo el remito y extrayendo las líneas…
            </div>
          ) : (
            lines.map((line, index) => (
              <div
                key={index}
                className="grid grid-cols-[1fr_0.8fr_0.6fr_0.7fr_auto] items-start gap-2"
              >
                <Input
                  type="text"
                  placeholder="Ej: Yerba 1kg"
                  value={line.product_name}
                  onChange={(e) => setLine(index, "product_name", e.target.value)}
                />
                <Input
                  type="text"
                  placeholder="—"
                  value={line.sku}
                  onChange={(e) => setLine(index, "sku", e.target.value)}
                />
                <Input
                  type="number"
                  min={0}
                  step="any"
                  placeholder="0"
                  value={line.qty}
                  onChange={(e) => setLine(index, "qty", e.target.value)}
                />
                <Input
                  type="number"
                  min={0}
                  step="any"
                  placeholder="0"
                  value={line.unit_price}
                  onChange={(e) => setLine(index, "unit_price", e.target.value)}
                />
                <button
                  type="button"
                  title="Quitar línea"
                  aria-label="Quitar línea"
                  disabled={lines.length === 1}
                  onClick={() => removeLine(index)}
                  className="mt-0.5 inline-flex h-10 w-10 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-40"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            ))
          )}

          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={extracting}
            onClick={addLine}
          >
            <Plus className="h-4 w-4" />
            Agregar línea
          </Button>
        </div>

        <div className="max-w-xs">
          <Input
            label="Costo de envío (opcional)"
            type="number"
            min={0}
            step="any"
            placeholder="0"
            value={shipping}
            onChange={(e) => {
              setShipping(e.target.value);
              if (error) setError(null);
            }}
            hint="Se registra como gasto de logística."
          />
        </div>

        {error ? <p className="text-xs text-vk-danger">{error}</p> : null}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" size="sm" onClick={onClose}>
            Cancelar
          </Button>
          <Button
            type="submit"
            size="sm"
            loading={uploadMutation.isPending}
            disabled={extracting}
          >
            Guardar remito
          </Button>
        </div>
      </form>
    </Modal>
  );
}
