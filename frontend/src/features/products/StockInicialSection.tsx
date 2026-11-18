"use client";

// «Stock inicial» dentro de «Editar producto»: desde cuándo figura el stock del producto y,
// si se cargó sin costo, el costo que le falta. La fecha y el costo son dos acciones
// separadas, cada una con su PIN. Un costo que ya existe no se corrige acá (tocaría ventas
// ya costeadas y márgenes); la fecha de una compra ya cargada tampoco.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { FilaConFecha } from "@/features/dashboard/AperturasPorConfirmar";
import { productsService, type AperturaDelProducto } from "@/services/products.service";
import { confirmarAperturaDeProducto } from "@/services/dashboard.service";
import { esPinCancelado } from "@/stores/pinGateStore";

const campo =
  "rounded border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary";

function fechaCorta(iso: string | null): string {
  if (!iso) return "sin fecha";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}

const moneda = new Intl.NumberFormat("es-AR", { style: "currency", currency: "ARS" });

/** El motivo del servidor (404/409 dicen qué hacer); si no hay, uno genérico. */
function mensajeDeError(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const m = (detail as { message?: unknown }).message;
    if (typeof m === "string" && m) return m;
  }
  return "No se pudo guardar el costo. Probá de nuevo.";
}

const ORIGEN: Record<AperturaDelProducto["origen"], string> = {
  usuario: "Fecha confirmada por vos.",
  archivo: "La fecha vino en el archivo; nadie la confirmó todavía.",
  carga: "Es la fecha en que se cargó el archivo, no necesariamente cuándo lo compraste.",
};

function CostoQueFalta({
  productId,
  costoDelProducto,
  alGuardar,
}: {
  productId: string;
  /** Costo de reposición del producto: sólo sugiere, nunca se guarda solo. */
  costoDelProducto: number | null;
  alGuardar: () => Promise<void>;
}) {
  const [texto, setTexto] = useState("");
  const [mensaje, setMensaje] = useState<string | null>(null);
  const valor = Number(texto.replace(",", "."));
  const valido = texto.trim() !== "" && Number.isFinite(valor) && valor > 0;
  const mutation = useMutation({
    mutationFn: () => productsService.setOpeningCost(productId, valor.toFixed(2)),
    onSuccess: async (r) => {
      const n = r.ventas_sin_costo_desde_la_fecha;
      setMensaje(
        n === 0
          ? "Listo: el stock inicial ya tiene costo."
          : `Listo: el stock inicial ya tiene costo y ${n} ${n === 1 ? "venta sin costo se costea" : "ventas sin costo se costean"}.`,
      );
      await alGuardar();
    },
    onError: (e) => {
      if (esPinCancelado(e)) return;
      setMensaje(mensajeDeError(e));
    },
  });

  return (
    <div className="space-y-2 rounded-lg border border-vektor-border px-3 py-2">
      <p className="text-sm text-vektor-body">Este stock se cargó sin costo</p>
      <p className="text-xs text-vektor-muted">
        Sin costo no respalda ventas, aunque cambies la fecha. Cargá el costo por unidad de venta.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <input
          aria-label="Costo del stock inicial"
          className={`${campo} w-36`}
          inputMode="decimal"
          placeholder="0,00"
          value={texto}
          onChange={(e) => {
            setTexto(e.target.value);
            setMensaje(null);
          }}
        />
        {costoDelProducto !== null && costoDelProducto > 0 && (
          <button
            type="button"
            onClick={() => setTexto(String(costoDelProducto))}
            className="h-9 rounded-lg border border-vektor-border px-3 text-xs text-vektor-body"
          >
            Usar el costo del producto ({moneda.format(costoDelProducto)})
          </button>
        )}
        <button
          type="button"
          disabled={!valido || mutation.isPending}
          onClick={() => mutation.mutate()}
          className="h-9 rounded-lg bg-vektor-blue px-3 text-xs font-medium text-white disabled:opacity-40"
        >
          Cargar costo
        </button>
      </div>
      {mensaje && <p className="text-xs text-vektor-muted">{mensaje}</p>}
    </div>
  );
}

export function StockInicialSection({
  productId,
  costoDelProducto,
}: {
  productId: string;
  costoDelProducto: number | null;
}) {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["product-detail", productId],
    queryFn: () => productsService.getProduct(productId),
  });
  // Un servidor viejo no trae `apertura`: la sección no aparece (nada se rompe).
  if (isLoading || !data) return null;
  const ap = data.apertura;
  if (!ap) {
    if (!data.primer_dia_de_fuente) return null;
    return (
      <section className="space-y-1 rounded-lg border border-vektor-border px-3 py-2">
        <h3 className="text-sm font-medium text-vektor-white">Stock inicial</h3>
        <p className="text-xs text-vektor-muted">
          Este producto no tiene stock inicial cargado: su primera fuente de costo es una compra
          del {fechaCorta(data.primer_dia_de_fuente)}, y esa fecha no se edita desde acá.
        </p>
      </section>
    );
  }

  const refrescar = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["product-detail", productId] }),
      qc.invalidateQueries({ queryKey: ["products"] }),
      qc.invalidateQueries({ queryKey: ["margins"] }),
      qc.invalidateQueries({ queryKey: ["others-link-queue"] }),
    ]);
  };

  return (
    <section className="grid gap-3" aria-label="Stock inicial">
      <div>
        <h3 className="text-sm font-medium text-vektor-white">Stock inicial</h3>
        <p className="text-xs text-vektor-muted">
          Figura desde el {fechaCorta(ap.fecha)}. {ORIGEN[ap.origen]}
          {ap.con_costo && ap.costo !== null
            ? ` Costo: ${moneda.format(Number(ap.costo))} por unidad.`
            : ""}
        </p>
      </div>

      {!ap.con_costo && (
        <CostoQueFalta
          productId={productId}
          costoDelProducto={costoDelProducto}
          alGuardar={refrescar}
        />
      )}

      {ap.editable ? (
        <ul className="grid gap-2">
          <FilaConFecha
            titulo="Desde cuándo tenés este stock"
            detalle={
              "Poné la fecha real de la primera compra. Más temprano: respalda ventas anteriores " +
              "a la carga y las que estaban sin costo se costean solas. Más tarde: las ventas ya " +
              "costeadas antes de esa fecha conservan su costo y se te avisa cuáles."
            }
            confirmar={(fecha) => confirmarAperturaDeProducto(productId, fecha)}
            alConfirmar={refrescar}
          />
        </ul>
      ) : (
        <p className="text-xs text-vektor-muted">
          {ap.motivo_no_editable ?? "La fecha de este stock no se puede cambiar desde acá."}
        </p>
      )}
    </section>
  );
}
