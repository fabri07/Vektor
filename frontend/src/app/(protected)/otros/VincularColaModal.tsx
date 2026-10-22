"use client";

// B·8b: la cola «Vincular a producto». Las ventas que el import mandó a «Otros»
// porque no supo qué producto eran se resuelven de a UNA referencia (lo que
// decía el archivo, «Coca 500»): la persona elige el producto y todas las ventas
// que dicen lo mismo se vinculan con él. El backend (`link-product`, B·8a)
// aprende el alias en la primera; en las demás el alias ya es del mismo producto
// y no se toca. Nada masivo silencioso: cada referencia la decide una persona.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import {
  othersService,
  type UnclassifiedRecordResponse,
} from "@/services/others.service";
import { productsService } from "@/services/products.service";
import { agruparPorReferencia } from "./helpers";
import { ProductoVendidoSelector } from "./ProductoVendidoSelector";

/** Tope de ventas que se traen para armar la cola (un negocio puede tener miles). */
export const TOPE_COLA = 5000;

interface DetalleDeError {
  code?: string;
  message?: string;
  product_name?: string | null;
}

interface CuerpoDeError {
  detail?: unknown;
  // El 400 de stock insuficiente trae `code` y las cantidades al lado de `detail`
  // (que ahí es el texto), no adentro.
  code?: string;
  available?: number | string;
  requested?: number | string;
}

function detalle(error: unknown): {
  status?: number;
  detail?: DetalleDeError;
  cuerpo?: CuerpoDeError;
} {
  const res = (error as { response?: { status?: number; data?: CuerpoDeError } })?.response;
  const d = res?.data?.detail;
  return {
    status: res?.status,
    detail: d && typeof d === "object" && !Array.isArray(d) ? (d as DetalleDeError) : undefined,
    cuerpo: res?.data,
  };
}

export function codigoDeError(error: unknown): string | null {
  const { detail, cuerpo } = detalle(error);
  return detail?.code ?? cuerpo?.code ?? null;
}

/** Cada respuesta de `link-product` en español, sin jerga. */
export function mensajeVincular(error: unknown): string {
  const { status, detail, cuerpo } = detalle(error);
  if (cuerpo?.code === "INSUFFICIENT_STOCK") {
    if (typeof cuerpo.detail === "string" && cuerpo.detail.trim()) return cuerpo.detail;
    return (
      `No hay stock suficiente para esta venta: hay ${cuerpo.available ?? "—"} y la venta ` +
      `pide ${cuerpo.requested ?? "—"}.`
    );
  }
  switch (detail?.code) {
    case "IMPORT_IDENTITY_TAKEN":
      return (
        "Esta venta ya está cargada (otra fila del mismo comprobante/operación ya se " +
        "registró). Descartala o revisala desde la lista."
      );
    case "SALE_WITHOUT_PURCHASE":
      return (
        `Ese producto${detail.product_name ? ` («${detail.product_name}»)` : ""} no tiene ` +
        "compra ni stock inicial antes de la fecha de la venta: cargá la compra o el " +
        "stock inicial primero."
      );
    case "ALIAS_OTRO_PRODUCTO":
    case "ALIAS_NOMBRE_DE_OTRO_PRODUCTO":
      // El backend nombra el producto al que ya pertenece ese texto.
      return detail.message ?? "Ese nombre ya es de otro producto del catálogo.";
    case "CANTIDAD_REQUERIDA":
      return "La cantidad de esta venta no es un número entero: escribila en la unidad de venta.";
    case "RECORD_ALREADY_RESOLVED":
      return "Esta venta ya se había resuelto.";
    case "NOT_A_SALE":
      return "Esta fila no es una venta: resolvela desde «Editar».";
    case "SIN_REFERENCIA":
      return "La fila no dice qué producto se vendió: resolvela desde «Editar».";
    case "INVALID_TARGET_PRODUCT":
      return "Ese producto ya no está activo: elegí otro.";
  }
  if (status === 404) return "Esta venta ya no está en «Otros».";
  if (status === 403) {
    return "No tenés permiso para vincular ventas: lo hace el dueño o un administrador.";
  }
  if (detail?.message) return detail.message;
  if (status === 422) {
    return "A la fila le falta la fecha o el monto para registrar la venta: resolvela desde «Editar».";
  }
  return "No se pudo vincular la venta. Probá de nuevo.";
}

interface FilaConFalla {
  record: UnclassifiedRecordResponse;
  code: string | null;
  mensaje: string;
  cantidad: string;
}

const inputCls =
  "rounded border border-vk-border-w bg-vk-bg-light px-3 py-2 text-sm text-vk-text-primary";

function formatoMonto(raw: string | undefined): string {
  const num = Number(String(raw ?? "").replace(/[^\d.,-]/g, "").replace(",", "."));
  return raw && Number.isFinite(num)
    ? new Intl.NumberFormat("es-AR", {
        style: "currency",
        currency: "ARS",
        maximumFractionDigits: 0,
      }).format(num)
    : "—";
}

export function VincularColaModal({ onClose }: { onClose: () => void }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["others-link-queue"],
    queryFn: () => othersService.getPendingSalesWithReference(TOPE_COLA),
    staleTime: 0,
    refetchOnWindowFocus: false,
  });
  const { data: productos = [], isError: productosError } = useQuery({
    queryKey: ["products-list"],
    queryFn: () => productsService.getAllProducts({ is_active: true }),
    staleTime: 60 * 1000,
  });

  // La cola se arma una vez al abrir: vincular no la reordena bajo los pies.
  const grupos = useMemo(() => agruparPorReferencia(data?.records ?? []), [data]);
  const [idx, setIdx] = useState(0);
  const grupo = grupos[idx];

  const [productoId, setProductoId] = useState("");
  const [busqueda, setBusqueda] = useState("");
  const [corriendo, setCorriendo] = useState(false);
  const [progreso, setProgreso] = useState<{ hecho: number; total: number } | null>(null);
  const [errorPrincipal, setErrorPrincipal] = useState<string | null>(null);
  // El primer intento pidió cantidad (la fila no trae un entero).
  const [cantidadPrimero, setCantidadPrimero] = useState<string | null>(null);
  const [fallas, setFallas] = useState<FilaConFalla[]>([]);
  const [hechas, setHechas] = useState<Set<string>>(new Set());
  const [vinculadas, setVinculadas] = useState(0);
  const [aviso, setAviso] = useState<string | null>(null);

  // Cerrar a mitad de camino corta lo que falta: cada llamada es atómica, así que
  // lo ya vinculado queda bien y lo demás sigue en «Otros».
  const cancelado = useRef(false);
  useEffect(
    () => () => {
      cancelado.current = true;
    },
    [],
  );

  useEffect(() => {
    setProductoId("");
    setBusqueda(grupo?.referencia ?? "");
    setErrorPrincipal(null);
    setCantidadPrimero(null);
    setFallas([]);
    setProgreso(null);
  }, [grupo]);

  const siguiente = useCallback(() => setIdx((i) => i + 1), []);

  const vincular = async () => {
    if (!grupo || !productoId || corriendo) return;
    if (cantidadPrimero !== null && !/^\d+$/.test(cantidadPrimero.trim())) {
      setErrorPrincipal("Escribí la cantidad vendida (un número entero en la unidad de venta).");
      return;
    }
    const pendientes = grupo.records.filter((r) => !hechas.has(r.id));
    setCorriendo(true);
    setErrorPrincipal(null);
    setAviso(null);
    const nuevasFallas: FilaConFalla[] = [];
    const nuevasHechas = new Set(hechas);
    let primeraOk = false;
    let ok = 0;
    for (const [i, record] of pendientes.entries()) {
      if (cancelado.current) return;
      setProgreso({ hecho: i + 1, total: pendientes.length });
      const usarCantidad = !primeraOk && cantidadPrimero !== null;
      try {
        await othersService.linkProduct(record.id, {
          product_id: productoId,
          ...(usarCantidad ? { quantity: Number(cantidadPrimero) } : {}),
        });
        primeraOk = true;
        ok += 1;
        nuevasHechas.add(record.id);
      } catch (error) {
        const code = codigoDeError(error);
        if (code === "RECORD_ALREADY_RESOLVED") {
          // Alguien la resolvió antes: no es una falla de esta vinculación.
          nuevasHechas.add(record.id);
          continue;
        }
        if (!primeraOk) {
          // Hasta que una salga bien, el error es de la elección (el producto, su
          // compra, el alias): se corta y se muestra, sin tocar las demás.
          if (code === "CANTIDAD_REQUERIDA") setCantidadPrimero("");
          setErrorPrincipal(mensajeVincular(error));
          break;
        }
        nuevasFallas.push({ record, code, mensaje: mensajeVincular(error), cantidad: "" });
      }
    }
    if (cancelado.current) return;
    setHechas(nuevasHechas);
    setVinculadas((n) => n + ok);
    setCorriendo(false);
    setProgreso(null);
    if (!primeraOk) return;
    const nombre = productos.find((p) => p.id === productoId)?.name ?? "el producto";
    setAviso(`Se vincularon ${ok} venta(s) de «${grupo.referencia}» a «${nombre}».`);
    if (nuevasFallas.length === 0) siguiente();
    else {
      setCantidadPrimero(null);
      setFallas(nuevasFallas);
    }
  };

  const reintentar = async (falla: FilaConFalla) => {
    if (corriendo) return;
    const cantidad = falla.cantidad.trim();
    if (falla.code === "CANTIDAD_REQUERIDA" && !/^\d+$/.test(cantidad)) return;
    setCorriendo(true);
    try {
      await othersService.linkProduct(falla.record.id, {
        product_id: productoId,
        ...(falla.code === "CANTIDAD_REQUERIDA" ? { quantity: Number(cantidad) } : {}),
      });
      if (cancelado.current) return;
      setHechas((prev) => new Set(prev).add(falla.record.id));
      setVinculadas((n) => n + 1);
      const resto = fallas.filter((f) => f.record.id !== falla.record.id);
      setFallas(resto);
      if (resto.length === 0) siguiente();
    } catch (error) {
      if (cancelado.current) return;
      setFallas((prev) =>
        prev.map((f) =>
          f.record.id === falla.record.id
            ? { ...f, code: codigoDeError(error), mensaje: mensajeVincular(error) }
            : f,
        ),
      );
    } finally {
      if (!cancelado.current) setCorriendo(false);
    }
  };

  const setCantidadFalla = (id: string, cantidad: string) =>
    setFallas((prev) => prev.map((f) => (f.record.id === id ? { ...f, cantidad } : f)));

  const pendientesDelGrupo = grupo ? grupo.records.filter((r) => !hechas.has(r.id)).length : 0;

  let cuerpo: React.ReactNode;
  if (isError) {
    cuerpo = (
      <p role="alert" className="text-sm text-vk-danger">
        No se pudieron traer las ventas pendientes. Cerrá y probá de nuevo.
      </p>
    );
  } else if (isLoading) {
    cuerpo = <p className="text-sm text-vk-text-muted">Buscando las ventas sin producto…</p>;
  } else if (grupos.length === 0) {
    cuerpo = (
      <p className="text-sm text-vk-text-muted">
        No hay ventas pendientes que digan qué producto se vendió. Las que no lo dicen se
        resuelven de a una desde «Editar».
      </p>
    );
  } else if (!grupo) {
    cuerpo = (
      <div className="grid gap-2 text-sm">
        {aviso ? <p className="text-vektor-teal">{aviso}</p> : null}
        <p className="text-vk-text-primary">
          Terminaste la cola: {vinculadas} venta(s) vinculadas. Las referencias que salteaste
          siguen en «Otros».
        </p>
      </div>
    );
  } else {
    cuerpo = (
      <form
        className="grid gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          void vincular();
        }}
      >
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="font-display text-xs uppercase tracking-wider text-vk-text-muted">
            {idx + 1} de {grupos.length} referencias
          </p>
          {data?.truncado ? (
            <p className="text-xs text-vk-text-muted">
              Se muestran las referencias de las primeras {TOPE_COLA.toLocaleString("es-AR")}{" "}
              ventas pendientes.
            </p>
          ) : null}
        </div>

        {aviso ? (
          <p role="status" className="text-sm text-vektor-teal">
            {aviso}
          </p>
        ) : null}

        <div className="rounded border border-vk-border-w bg-vk-bg-light p-3">
          <p className="text-xs text-vk-text-muted">El archivo decía</p>
          <p className="font-display text-xl text-vk-text-primary">«{grupo.referencia}»</p>
          <p className="text-xs text-vk-text-muted">
            {grupo.records.length === 1
              ? "La usa 1 venta"
              : `La usan ${grupo.records.length} ventas`}
            {pendientesDelGrupo !== grupo.records.length
              ? ` (quedan ${pendientesDelGrupo})`
              : ""}
          </p>
          <table className="mt-2 w-full text-left text-xs">
            <thead className="text-vk-text-muted">
              <tr>
                <th className="font-normal">Fecha</th>
                <th className="font-normal">Monto</th>
                <th className="font-normal">Cantidad</th>
              </tr>
            </thead>
            <tbody className="text-vk-text-primary">
              {grupo.records.slice(0, 3).map((r) => (
                <tr key={r.id}>
                  <td>{r.row_data?.fecha || "—"}</td>
                  <td>{formatoMonto(r.row_data?.monto)}</td>
                  <td>{r.row_data?.cantidad || "1"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {fallas.length === 0 ? (
          <div className="grid gap-2">
            <ProductoVendidoSelector
              productos={productos}
              busqueda={busqueda}
              onBusqueda={setBusqueda}
              value={productoId}
              onChange={setProductoId}
              referencia={grupo.referencia}
              inputClassName={inputCls}
            />
            {productosError ? (
              <p role="alert" className="text-xs text-vk-danger">
                No se pudo cargar el catálogo de productos.
              </p>
            ) : null}
            {cantidadPrimero !== null ? (
              <label className="grid gap-1 text-sm text-vektor-body">
                Cantidad vendida en la primera venta
                <input
                  aria-label="Cantidad vendida"
                  className={inputCls}
                  type="number"
                  min={1}
                  step={1}
                  value={cantidadPrimero}
                  onChange={(e) => setCantidadPrimero(e.target.value)}
                />
              </label>
            ) : null}
            {errorPrincipal ? (
              <p role="alert" className="text-sm text-vk-danger">
                {errorPrincipal}
              </p>
            ) : (
              <p className="text-xs text-vk-text-muted">
                Todas las ventas que dicen «{grupo.referencia}» se vinculan a ese producto, y
                el nombre queda aprendido para los próximos archivos.
              </p>
            )}
          </div>
        ) : (
          <div className="grid gap-2">
            <p className="text-sm text-vk-text-primary">
              Estas ventas no se pudieron vincular. Las demás ya quedaron.
            </p>
            <ul className="grid gap-2">
              {fallas.map((f) => (
                <li
                  key={f.record.id}
                  className="grid gap-1 rounded border border-vk-danger/30 p-2 text-xs"
                >
                  <span className="text-vk-text-primary">
                    {f.record.row_data?.fecha || "—"} · {formatoMonto(f.record.row_data?.monto)} ·
                    cantidad {f.record.row_data?.cantidad || "1"}
                  </span>
                  <span role="alert" className="text-vk-danger">
                    {f.mensaje}
                  </span>
                  <span className="flex items-center gap-2">
                    {f.code === "CANTIDAD_REQUERIDA" ? (
                      <input
                        aria-label={`Cantidad de la venta del ${f.record.row_data?.fecha ?? ""}`}
                        className={`${inputCls} w-24`}
                        type="number"
                        min={1}
                        step={1}
                        value={f.cantidad}
                        onChange={(e) => setCantidadFalla(f.record.id, e.target.value)}
                      />
                    ) : null}
                    <button
                      type="button"
                      disabled={corriendo}
                      onClick={() => void reintentar(f)}
                      className="rounded border border-vk-border-w px-2 py-1 text-vk-text-primary hover:bg-vk-surface-w disabled:opacity-50"
                    >
                      Reintentar
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {progreso ? (
          <p role="status" className="text-sm text-vk-text-muted">
            Vinculando {progreso.hecho} de {progreso.total}…
          </p>
        ) : null}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            disabled={corriendo}
            onClick={() => {
              setAviso(null);
              siguiente();
            }}
            className="rounded border border-vk-border-w px-4 py-2 text-sm text-vk-text-primary disabled:opacity-50"
          >
            {fallas.length > 0 ? "Siguiente referencia" : "Saltear"}
          </button>
          {fallas.length === 0 ? (
            <button
              type="submit"
              disabled={corriendo || !productoId}
              className="rounded bg-vk-blue px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
            >
              {corriendo ? "Vinculando…" : "Vincular"}
            </button>
          ) : null}
        </div>
      </form>
    );
  }

  return (
    <Modal isOpen onClose={onClose} title="Vincular ventas a productos" size="lg">
      {cuerpo}
    </Modal>
  );
}
