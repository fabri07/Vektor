"use client";

/**
 * B·5d: la tabla de stock inicial a mano (pantalla del lote de B·5c).
 *
 * Dos pasos, como el servidor: «Revisar» (`POST /products/batch/preview`, no
 * escribe) y «Confirmar» (`POST /products/batch`, todo o nada). Una fila que
 * se parece a un producto existente exige una elección explícita: nunca se
 * decide sola si «es ese» o «es uno nuevo». Editar la tabla después de
 * revisar invalida la revisión (el token del servidor es de ESAS filas).
 */

import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { parseNumeroAr } from "@/lib/numeroAr";
import { unidadDeVentaDe } from "@/features/products/saleUnitOptions";
import { Button } from "@/components/ui/Button";
import {
  EnvioSelect,
  QUE_ES_STOCK_INICIAL,
  fechaDeStockInicial,
  hoyLocal,
  nuevaClaveIdempotencia,
} from "@/features/products/costoManual";
import {
  productsService,
  type CoincidenciaDeFila,
  type EleccionDeFila,
  type FilaPreview,
  type FilaStockInicial,
  type LoteStockInicialPreview,
  type LoteStockInicialResponse,
  type TratamientoEnvio,
} from "@/services/products.service";
import { esPinCancelado } from "@/stores/pinGateStore";
import { posErrorInfo } from "@/lib/pos/errors";

interface FilaEditable {
  key: number;
  nombre: string;
  precio: string;
  costo: string;
  stock: string;
  sku: string;
}

/** Valor del radio «Es un producto nuevo». */
const NUEVO = "__crear__";

const NOMBRES_DE_CAMPO: Record<string, string> = {
  sku: "SKU",
  barcode: "código de barras",
  sale_price_ars: "precio",
  categoria: "categoría",
  nombre: "nombre",
};

const inputClass =
  "h-9 w-full min-w-0 rounded-lg border border-vk-border-w bg-vk-surface-w px-2 text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/15 focus:border-vk-blue/40";

let siguienteKey = 1;
function filaVacia(): FilaEditable {
  return { key: siguienteKey++, nombre: "", precio: "", costo: "", stock: "", sku: "" };
}

/** «1.000» es mil (montos argentinos); lo que no se entiende es NaN. */
function numero(texto: string): number {
  return parseNumeroAr(texto) ?? Number.NaN;
}

/**
 * En qué se lee el stock de una fila que se aplica a un producto existente: su
 * unidad de VENTA. Por kilo / litro (factor 1000) son kg / L; «gram» o
 * «milliliter» con factor 1 se venden por gramo / ml.
 */
function unidadDeLaFila(c: CoincidenciaDeFila): string | null {
  const factor = c.base_units_per_sale_unit ?? 1;
  if (c.sale_unit === "gram") return factor === 1000 ? unidadDeVentaDe("gram", 1000).etiqueta : "gramos";
  if (c.sale_unit === "milliliter") return factor === 1000 ? unidadDeVentaDe("milliliter", 1000).etiqueta : "ml";
  return null;
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

/** Errores propios de la fila, antes de preguntarle al servidor. */
function erroresLocales(f: FilaEditable): string[] {
  const out: string[] = [];
  if (!f.nombre.trim()) out.push("Falta el nombre.");
  if (!(numero(f.precio) > 0)) out.push("El precio de venta tiene que ser mayor a 0.");
  if (!(numero(f.costo) > 0)) out.push("El costo es obligatorio y mayor a 0.");
  const stock = numero(f.stock);
  if (!Number.isInteger(stock) || stock < 1) out.push("El stock tiene que ser un número entero, 1 o más.");
  return out;
}

function aPayload(f: FilaEditable, envio: TratamientoEnvio | ""): FilaStockInicial {
  return {
    nombre: f.nombre.trim(),
    ...(f.sku.trim() ? { sku: f.sku.trim() } : {}),
    sale_price_ars: round2(numero(f.precio)),
    unit_cost_ars: round2(numero(f.costo)),
    stock_units: numero(f.stock),
    ...(envio ? { costo_envio: envio } : {}),
  };
}

/** ¿Se puede crear como producto nuevo? No con el SKU o el código de otro. */
function admiteNuevo(p: FilaPreview): boolean {
  return !p.coincidencias.some((c) => c.por === "sku" || c.por === "barcode");
}

function tieneOpcion(p: FilaPreview): boolean {
  return admiteNuevo(p) || p.coincidencias.some((c) => c.necesita_stock_inicial);
}

export function StockInicialTabla({
  onDone,
}: {
  /** `null` = esa carga ya se había hecho (409 `DUPLICATE_IDEMPOTENT`): no hay detalle. */
  onDone?: (r: LoteStockInicialResponse | null) => void;
}) {
  const queryClient = useQueryClient();
  // La MISMA clave mientras el cuerpo no cambie: si la respuesta se pierde y
  // el servidor ya commiteó, el reintento no duplica productos ni stock.
  const claveDelIntento = useRef<{ firma: string; clave: string } | null>(null);
  const [yaHecha, setYaHecha] = useState(false);
  const [filas, setFilas] = useState<FilaEditable[]>(() => [filaVacia()]);
  const [fecha, setFecha] = useState("");
  const [envio, setEnvio] = useState<TratamientoEnvio | "">("");
  const [preview, setPreview] = useState<LoteStockInicialPreview | null>(null);
  // fila → product_id elegido, o NUEVO.
  const [elecciones, setElecciones] = useState<Record<number, string>>({});
  const [locales, setLocales] = useState<Record<number, string[]>>({});
  const [mensaje, setMensaje] = useState<{ tipo: "error" | "aviso"; texto: string } | null>(null);
  const [enviando, setEnviando] = useState(false);
  const [resultado, setResultado] = useState<LoteStockInicialResponse | null>(null);
  // Las filas tal como se mandaron a confirmar: el resumen las nombra.
  const [confirmadas, setConfirmadas] = useState<FilaStockInicial[]>([]);

  /** Toda edición de la tabla invalida la revisión hecha. */
  function invalidar() {
    if (preview) {
      setPreview(null);
      setElecciones({});
      setMensaje({ tipo: "aviso", texto: "Cambiaste la tabla: volvé a revisarla antes de confirmar." });
    }
    setLocales({});
  }

  function editar(key: number, campo: keyof Omit<FilaEditable, "key">, valor: string) {
    setFilas((prev) => prev.map((f) => (f.key === key ? { ...f, [campo]: valor } : f)));
    invalidar();
  }

  function agregar() {
    setFilas((prev) => [...prev, filaVacia()]);
    invalidar();
  }

  function quitar(key: number) {
    setFilas((prev) => (prev.length > 1 ? prev.filter((f) => f.key !== key) : prev));
    invalidar();
  }

  async function revisar() {
    if (enviando) return;
    setMensaje(null);
    const porFila: Record<number, string[]> = {};
    filas.forEach((f, i) => {
      const e = erroresLocales(f);
      if (e.length) porFila[i] = e;
    });
    setLocales(porFila);
    if (Object.keys(porFila).length) {
      setMensaje({ tipo: "error", texto: "Hay filas incompletas: revisalas." });
      return;
    }
    if (filas.length > 200) {
      setMensaje({ tipo: "error", texto: "Hasta 200 filas por carga." });
      return;
    }
    setEnviando(true);
    try {
      const p = await productsService.previewInitialStockBatch(filas.map((f) => aPayload(f, envio)));
      setPreview(p);
      setElecciones({});
    } catch (e) {
      if (!esPinCancelado(e)) setMensaje({ tipo: "error", texto: textoDeError(e) });
    } finally {
      setEnviando(false);
    }
  }

  const fechaError =
    fecha === ""
      ? "Indicá desde qué fecha tenés este stock."
      : fecha > hoyLocal()
        ? "La fecha no puede ser futura."
        : null;
  const pendientes = preview
    ? preview.filas.filter((p) => p.coincidencias.length > 0 && !elecciones[p.fila])
    : [];
  const conErrores = preview ? preview.filas.filter((p) => p.errores.length > 0) : [];
  const sinOpcion = preview ? preview.filas.filter((p) => p.coincidencias.length > 0 && !tieneOpcion(p)) : [];
  const puedeConfirmar =
    !!preview && !enviando && fechaError === null && pendientes.length === 0 && conErrores.length === 0 && sinOpcion.length === 0;

  async function confirmar() {
    if (!preview || !puedeConfirmar) return;
    setMensaje(null);
    const payloadFilas = filas.map((f) => aPayload(f, envio));
    const eleccionesPayload: EleccionDeFila[] = Object.entries(elecciones).map(([fila, valor]) =>
      valor === NUEVO
        ? { fila: Number(fila), accion: "crear" }
        : { fila: Number(fila), accion: "apertura_en", product_id: valor },
    );
    const body = {
      filas: payloadFilas,
      elecciones: eleccionesPayload,
      preview_token: preview.preview_token,
      fecha: fechaDeStockInicial(fecha),
    };
    // Mismo cuerpo (filas + elecciones + token + fecha + envío) = mismo
    // intento = misma clave. Cualquier cambio es otro intento.
    const firma = JSON.stringify(body);
    if (claveDelIntento.current?.firma !== firma) {
      claveDelIntento.current = { firma, clave: nuevaClaveIdempotencia() };
    }
    setEnviando(true);
    try {
      const r = await productsService.confirmInitialStockBatch(body, claveDelIntento.current.clave);
      setConfirmadas(payloadFilas);
      setResultado(r);
      onDone?.(r);
    } catch (e) {
      if (esPinCancelado(e)) return;
      const info = posErrorInfo(e);
      if (info.code === "DUPLICATE_IDEMPOTENT") {
        // El servidor ya registró ESTA carga (la respuesta anterior se perdió):
        // no se reintenta, se muestra hecha.
        void queryClient.invalidateQueries({ queryKey: ["products-list"] });
        setYaHecha(true);
        onDone?.(null);
      } else if (info.code === "BATCH_CHANGED") {
        const nuevo = info.fields.preview as LoteStockInicialPreview | undefined;
        if (nuevo) {
          // Se conservan sólo las elecciones que siguen siendo válidas.
          setElecciones((prev) => {
            const out: Record<number, string> = {};
            for (const p of nuevo.filas) {
              const v = prev[p.fila];
              if (!v) continue;
              const valida =
                v === NUEVO
                  ? admiteNuevo(p)
                  : p.coincidencias.some((c) => c.product_id === v && c.necesita_stock_inicial);
              if (valida) out[p.fila] = v;
            }
            return out;
          });
          setPreview(nuevo);
        } else {
          setPreview(null);
          setElecciones({});
        }
        setMensaje({
          tipo: "aviso",
          texto: "Los productos cambiaron desde que revisaste la tabla. Revisá las coincidencias de nuevo.",
        });
      } else if (info.code === "PRODUCTS_BEING_CREATED") {
        setMensaje({
          tipo: "aviso",
          texto: "Se están creando o cargando productos ahora mismo (otra carga o un archivo). Probá de nuevo en un momento.",
        });
      } else if (info.code === "ROWS_INVALID") {
        const filasErr = (info.fields.filas as { fila: number; errores: string[] }[] | undefined) ?? [];
        if (preview) {
          setPreview({
            ...preview,
            filas: preview.filas.map((p) => {
              const err = filasErr.find((x) => x.fila === p.fila);
              return err ? { ...p, errores: err.errores } : p;
            }),
          });
        }
        setMensaje({ tipo: "error", texto: "Hay filas con errores." });
      } else {
        setMensaje({ tipo: "error", texto: textoDeError(e) });
      }
    } finally {
      setEnviando(false);
    }
  }

  function otraCarga() {
    setFilas([filaVacia()]);
    setFecha("");
    setEnvio("");
    setPreview(null);
    setElecciones({});
    setLocales({});
    setMensaje(null);
    setResultado(null);
    setConfirmadas([]);
    setYaHecha(false);
    claveDelIntento.current = null;
  }

  if (yaHecha) {
    return (
      <div className="space-y-3" aria-live="polite">
        <p className="rounded-lg border border-vk-success/20 bg-vk-success-bg px-3 py-2 text-sm text-vk-success">
          Esa carga ya se hizo: el stock inicial quedó registrado. Revisalo en Productos.
        </p>
        <Button type="button" size="sm" variant="secondary" onClick={otraCarga}>
          Cargar otra tabla
        </Button>
      </div>
    );
  }

  if (resultado) {
    return (
      <div className="space-y-3" aria-live="polite">
        <p className="rounded-lg border border-vk-success/20 bg-vk-success-bg px-3 py-2 text-sm text-vk-success">
          Stock inicial cargado: {resultado.creados} producto(s) nuevo(s) y {resultado.con_apertura}{" "}
          producto(s) existente(s).
        </p>
        {resultado.filas.some((r) => r.campos_no_aplicados.length > 0) && (
          <ul className="space-y-1 text-xs text-vk-text-secondary">
            {resultado.filas
              .filter((r) => r.campos_no_aplicados.length > 0)
              .map((r) => (
                <li key={r.fila}>
                  {confirmadas[r.fila]?.nombre ?? `Fila ${r.fila + 1}`}: se cargó sólo el stock inicial; no se
                  cambió: {r.campos_no_aplicados.map((c) => NOMBRES_DE_CAMPO[c] ?? c).join(", ")}. Si querés
                  cambiarlo, editá el producto.
                </li>
              ))}
          </ul>
        )}
        <Button type="button" size="sm" variant="secondary" onClick={otraCarga}>
          Cargar más
        </Button>
      </div>
    );
  }

  return (
    // Con un pedido en vuelo nada se edita ni se reenvía: `disabled` en el
    // fieldset apaga todos los controles de adentro.
    <fieldset disabled={enviando} aria-busy={enviando} className="m-0 min-w-0 space-y-4 border-0 p-0">
      <p className="rounded-lg border border-vk-border-w bg-vk-bg-light px-3 py-2 text-xs text-vk-text-secondary">
        {QUE_ES_STOCK_INICIAL}
      </p>

      <div className="flex flex-col gap-1.5">
        <label htmlFor="stock-inicial-fecha" className="text-xs font-medium text-vektor-body">
          ¿Desde qué fecha tenés este stock?
        </label>
        <input
          id="stock-inicial-fecha"
          type="date"
          max={hoyLocal()}
          value={fecha}
          onChange={(e) => setFecha(e.target.value)}
          className={inputClass}
        />
        {fecha !== "" && fechaError && <p className="text-xs text-vk-danger">{fechaError}</p>}
      </div>

      <div className="space-y-3">
        {filas.map((f, i) => {
          const p = preview?.filas.find((x) => x.fila === i);
          const errs = [...(locales[i] ?? []), ...(p?.errores ?? [])];
          return (
            <fieldset
              key={f.key}
              className="space-y-2 rounded-lg border border-vk-border-w p-3"
              aria-label={`Fila ${i + 1}`}
            >
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-[2fr_1fr_1fr_1fr_1fr_auto]">
                <input
                  aria-label={`Nombre, fila ${i + 1}`}
                  placeholder="Nombre"
                  className={`${inputClass} col-span-2 sm:col-span-1`}
                  value={f.nombre}
                  maxLength={300}
                  onChange={(e) => editar(f.key, "nombre", e.target.value)}
                />
                <input
                  aria-label={`Precio de venta, fila ${i + 1}`}
                  placeholder="Precio de venta"
                  inputMode="decimal"
                  className={inputClass}
                  value={f.precio}
                  onChange={(e) => editar(f.key, "precio", e.target.value)}
                />
                <input
                  aria-label={`Costo, fila ${i + 1}`}
                  placeholder="Costo"
                  inputMode="decimal"
                  className={inputClass}
                  value={f.costo}
                  onChange={(e) => editar(f.key, "costo", e.target.value)}
                />
                <input
                  aria-label={`Stock, fila ${i + 1}`}
                  placeholder="Stock (u.)"
                  inputMode="numeric"
                  className={inputClass}
                  value={f.stock}
                  onChange={(e) => editar(f.key, "stock", e.target.value)}
                />
                <input
                  aria-label={`SKU, fila ${i + 1}`}
                  placeholder="SKU (opcional)"
                  className={inputClass}
                  maxLength={100}
                  value={f.sku}
                  onChange={(e) => editar(f.key, "sku", e.target.value)}
                />
                <button
                  type="button"
                  aria-label={`Quitar fila ${i + 1}`}
                  disabled={filas.length === 1}
                  onClick={() => quitar(f.key)}
                  className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-vk-border-w text-vk-text-muted hover:text-vk-danger disabled:opacity-40"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>

              {errs.length > 0 && (
                <ul className="space-y-0.5 text-xs text-vk-danger">
                  {errs.map((e) => (
                    <li key={e}>{e}</li>
                  ))}
                </ul>
              )}

              {p && p.coincidencias.length > 0 && (
                <div
                  role="radiogroup"
                  aria-label={`¿Qué producto es la fila ${i + 1}?`}
                  className="space-y-1 rounded-md bg-vk-bg-light p-2 text-xs text-vektor-body"
                >
                  <p className="font-medium">Se parece a un producto que ya tenés. ¿Cuál es?</p>
                  {p.coincidencias.map((c) =>
                    c.necesita_stock_inicial ? (
                      <label key={c.product_id} className="flex items-center gap-2">
                        <input
                          type="radio"
                          name={`eleccion-${f.key}`}
                          checked={elecciones[i] === c.product_id}
                          onChange={() => setElecciones((prev) => ({ ...prev, [i]: c.product_id }))}
                        />
                        Es «{c.nombre}»{c.por !== "nombre" ? ` (mismo ${NOMBRES_DE_CAMPO[c.por]})` : ""}
                        {unidadDeLaFila(c) ? (
                          <span className="text-vk-text-muted">
                            {" "}— el stock de la fila va en {unidadDeLaFila(c)}
                          </span>
                        ) : null}
                      </label>
                    ) : (
                      <p key={c.product_id} className="pl-5 text-vk-text-muted">
                        «{c.nombre}»: ya tiene stock inicial
                      </p>
                    ),
                  )}
                  <label className={`flex items-center gap-2 ${admiteNuevo(p) ? "" : "text-vk-text-muted"}`}>
                    <input
                      type="radio"
                      name={`eleccion-${f.key}`}
                      disabled={!admiteNuevo(p)}
                      checked={elecciones[i] === NUEVO}
                      onChange={() => setElecciones((prev) => ({ ...prev, [i]: NUEVO }))}
                    />
                    Es un producto nuevo
                    {!admiteNuevo(p) && " (no: tiene el SKU o el código de barras de otro producto)"}
                  </label>
                  {!tieneOpcion(p) && (
                    <p className="text-vk-danger">
                      Esta fila no se puede cargar acá: ese producto ya tiene stock inicial. Quitala de la tabla.
                    </p>
                  )}
                </div>
              )}
            </fieldset>
          );
        })}
      </div>

      <button
        type="button"
        onClick={agregar}
        className="inline-flex items-center gap-1 text-xs font-medium text-vk-blue hover:underline"
      >
        <Plus className="h-3.5 w-3.5" /> Agregar fila
      </button>

      <EnvioSelect value={envio} onChange={(v) => { setEnvio(v); invalidar(); }} />

      {mensaje && (
        <p
          role={mensaje.tipo === "error" ? "alert" : "status"}
          className={mensaje.tipo === "error" ? "text-xs text-vk-danger" : "text-xs text-vk-warning"}
        >
          {mensaje.texto}
        </p>
      )}

      {preview && pendientes.length > 0 && (
        <p className="text-xs text-vk-text-secondary">
          Elegí qué producto es cada fila marcada para poder confirmar.
        </p>
      )}

      <div className="flex gap-2">
        <Button
          type="button"
          size="sm"
          variant="secondary"
          disabled={enviando}
          loading={enviando && !preview}
          onClick={revisar}
        >
          Revisar
        </Button>
        <Button
          type="button"
          size="sm"
          disabled={!puedeConfirmar}
          loading={enviando && !!preview}
          onClick={confirmar}
        >
          Confirmar
        </Button>
      </div>
    </fieldset>
  );
}

function textoDeError(e: unknown): string {
  const info = posErrorInfo(e);
  if (info.message) return info.message;
  if (info.status === 422) return "Hay datos inválidos en la tabla: revisá precios, costos y stock.";
  if (info.status === null) return "No se pudo conectar con el servidor. Probá de nuevo.";
  return "No se pudo cargar el stock inicial.";
}
