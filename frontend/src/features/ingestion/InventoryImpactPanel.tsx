"use client";

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/Button";
import { useToastStore } from "@/stores/toastStore";
import type {
  InventoryImpactItem,
  InventoryReplayResult,
} from "@/services/ingestion.service";
import { ingestionService } from "@/services/ingestion.service";

/**
 * F-H3.c — el impacto que el archivo TENDRÍA sobre el inventario.
 *
 * El punto del panel es que el usuario pueda decidir si aplicar la historia del
 * archivo, así que muestra la cuenta completa (saldo inicial → movimientos →
 * saldo final) y no sólo el resultado: un saldo final solo no deja ver si el
 * número viene de las compras, de las ventas o de un catálogo que declaró otro
 * saldo.
 *
 * Nada de esto se aplicó. El copy lo dice en presente condicional a propósito
 * ("quedaría", "pasaría") — decirlo en pasado sugeriría que el stock ya cambió.
 *
 * F-H3.d.5 — con `fileId` el panel deja aplicarlo. Lo que se aplica son las
 * VENTAS: las compras del archivo ya subieron el stock al confirmar (por eso
 * aparecen en la columna "Compras" de la tabla y no vuelven a moverse). El
 * resultado que se muestra después es el que devolvió el servidor, recalculado
 * contra el stock del momento — no el de esta tabla, que puede haber quedado
 * vieja si entre el import y el clic pasó cualquier otra cosa.
 */

/**
 * Lo que deja de ser cierto cuando el replay descuenta las ventas.
 *
 * Aplicar escribe `InventoryMovement` y mueve `product.stock_units`, así que
 * todo lo que muestre stock queda sirviendo un número que el servidor ya
 * cambió. Mismo molde que `INVALIDATE_KEYS` en `useOfflineSubmit`: la lista se
 * escribe explícita en vez de invalidar todo, para no tirar cachés que esta
 * operación no toca.
 */
const CLAVES_A_INVALIDAR = [
  ["products-list"],
  ["products-all"],
  ["inventory"],
  ["inventory-effects"],
  ["ingestion-files"],
];

function formatearFecha(iso: string | null | undefined): string {
  if (!iso) return "";
  // Las fechas vienen como `YYYY-MM-DD` (día de negocio, sin hora). Se parte el
  // string en vez de usar `new Date`, que sobre un ISO sin zona interpreta UTC y
  // en Argentina puede mostrar el día anterior.
  const [anio, mes, dia] = iso.split("-");
  return dia && mes && anio ? `${dia}/${mes}/${anio}` : iso;
}

export function InventoryImpactPanel({
  items,
  total,
  fileId,
}: {
  items: InventoryImpactItem[];
  /** Total de productos con impacto, incluidos los que no se listan. */
  total: number;
  /** Con el id del archivo, el panel ofrece aplicar las ventas al inventario. */
  fileId?: string;
}) {
  const [aplicando, setAplicando] = useState(false);
  const [resultado, setResultado] = useState<InventoryReplayResult | null>(null);
  // Hojas del archivo que tienen ventas aplicables, descubiertas con el preview.
  // `null` = todavía no se preguntó. Se pide a demanda y no al montar: el cálculo
  // recorre todas las ventas del archivo y la mayoría de los imports no termina
  // en un replay.
  const [hojas, setHojas] = useState<string[] | null>(null);
  const [avisosPreview, setAvisosPreview] = useState<string[]>([]);
  const [elegidas, setElegidas] = useState<string[]>([]);
  const [descubriendo, setDescubriendo] = useState(false);
  const addToast = useToastStore((s) => s.add);
  const queryClient = useQueryClient();

  if (items.length === 0) return null;

  const conNegativo = items.filter((p) => p.primer_negativo_en);
  const ocultos = Math.max(0, total - items.length);
  const hayVentas = items.some((p) => p.vendidas > 0);

  /**
   * Preview read-only: qué hojas del archivo tienen ventas aplicables.
   *
   * El efecto de inventario es POR HOJA, así que aplicar el archivo entero
   * movería stock por filas que no hablan de unidades (una hoja de ventas de
   * servicios no descuenta nada). El backend rechaza un apply sin hojas
   * elegidas; esto es cómo se eligen.
   */
  async function descubrirHojas() {
    if (!fileId) return;
    setDescubriendo(true);
    try {
      const res = await ingestionService.applyInventoryReplay(fileId, {
        dryRun: true,
      });
      setHojas(res.hojas);
      setAvisosPreview(res.warnings);
      // Con una sola hoja no hay nada que elegir: se pre-tilda para no pedir un
      // clic que no decide nada.
      setElegidas(res.hojas.length === 1 ? res.hojas : []);
    } catch {
      addToast("No se pudieron leer las hojas del archivo.", "error");
    } finally {
      setDescubriendo(false);
    }
  }

  async function aplicar() {
    if (!fileId || elegidas.length === 0) return;
    setAplicando(true);
    try {
      const res = await ingestionService.applyInventoryReplay(fileId, {
        contextIds: elegidas,
      });
      setResultado(res);
      // Se invalida ante cualquier respuesta buena, sin mirar `aplicadas`: una
      // venta contada como `ya_aplicadas` pudo haberse descontado recién, en
      // vivo, por la otra rama. Refetchear de más cuesta una request; mostrar un
      // stock viejo después de una operación que lo movió es un número mal.
      for (const queryKey of CLAVES_A_INVALIDAR) {
        void queryClient.invalidateQueries({ queryKey });
      }
      addToast(
        res.aplicadas === 1
          ? "Se aplicó 1 venta al inventario."
          : `Se aplicaron ${res.aplicadas} ventas al inventario.`,
        res.sin_stock.length > 0 ? "warning" : "success",
      );
    } catch {
      addToast("No se pudo aplicar al inventario. Probá de nuevo.", "error");
    } finally {
      setAplicando(false);
    }
  }

  return (
    <section className="mt-3 rounded-lg border border-vektor-border bg-vektor-surface p-4">
      <h3 className="text-sm font-medium text-vektor-ink">
        Impacto en el inventario si aplicaras esta historia
      </h3>
      <p className="mt-1 text-xs text-vektor-ink/60">
        El stock <strong>no se modificó</strong>. Esto es lo que pasaría si
        aplicaras las compras y ventas del archivo, día por día.
      </p>

      {conNegativo.length > 0 && (
        <p className="mt-2 rounded border border-vk-warning/20 bg-vk-warning-bg px-3 py-2 text-xs text-vk-warning">
          {conNegativo.length === 1
            ? "1 producto quedaría"
            : `${conNegativo.length} productos quedarían`}{" "}
          con stock negativo en algún momento. Suele significar que falta cargar
          compras anteriores, no que el stock de hoy esté mal.
        </p>
      )}

      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[34rem] text-left text-xs">
          <thead className="text-vektor-ink/60">
            <tr>
              <th className="pb-2 pr-3 font-medium">Producto</th>
              <th className="pb-2 pr-3 text-right font-medium">Stock inicial</th>
              <th className="pb-2 pr-3 text-right font-medium">Compras</th>
              <th className="pb-2 pr-3 text-right font-medium">Ventas</th>
              <th className="pb-2 pr-3 text-right font-medium">Stock final</th>
              <th className="pb-2 font-medium">Mínimo</th>
            </tr>
          </thead>
          <tbody>
            {items.map((p) => {
              const tocaNegativo = Boolean(p.primer_negativo_en);
              return (
                <tr
                  key={p.product_id}
                  className="border-t border-vektor-border/50 text-vektor-ink"
                >
                  <td className="py-2 pr-3">{p.product_name}</td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {p.saldo_inicial}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {p.compradas > 0 ? `+${p.compradas}` : "—"}
                  </td>
                  <td className="py-2 pr-3 text-right tabular-nums">
                    {p.vendidas > 0 ? `−${p.vendidas}` : "—"}
                  </td>
                  <td
                    className={`py-2 pr-3 text-right tabular-nums ${
                      p.saldo_final < 0 ? "text-vk-danger" : ""
                    }`}
                  >
                    {p.saldo_final}
                  </td>
                  <td className="py-2 text-vektor-ink/70">
                    {tocaNegativo ? (
                      <span className="text-vk-warning">
                        {p.minimo} el {formatearFecha(p.primer_negativo_en)}
                      </span>
                    ) : (
                      p.minimo
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {ocultos > 0 && (
        <p className="mt-2 text-xs text-vektor-ink/60">
          Mostrando {items.length} de {total} productos con impacto (los que
          quedan en negativo aparecen primero).
        </p>
      )}

      {fileId && hayVentas && resultado === null && (
        <div className="mt-4 border-t border-vektor-border/50 pt-3">
          <p className="text-xs text-vektor-ink/70">
            Las compras del archivo ya se cargaron al stock. Lo que falta aplicar
            son las <strong>ventas</strong>: descontarlas deja el inventario como
            quedó después de esta historia.
          </p>

          {hojas === null ? (
            <Button
              size="sm"
              className="mt-2"
              disabled={descubriendo}
              onClick={() => void descubrirHojas()}
            >
              {descubriendo ? "Leyendo…" : "Elegir qué ventas aplicar"}
            </Button>
          ) : hojas.length === 0 ? (
            <p className="mt-2 text-xs text-vektor-ink/60">
              No quedan ventas por aplicar en este archivo.
            </p>
          ) : (
            <>
              <p className="mt-3 text-xs font-medium text-vektor-ink">
                ¿De qué hojas?
              </p>
              <p className="mt-0.5 text-xs text-vektor-ink/60">
                Cada hoja declaró su propio efecto al importar. Sólo se descuenta
                lo que marques.
              </p>
              <ul className="mt-2 space-y-1">
                {hojas.map((hoja) => (
                  <li key={hoja}>
                    <label className="flex items-center gap-2 text-xs text-vektor-ink">
                      <input
                        type="checkbox"
                        className="h-3.5 w-3.5 rounded border-vektor-border"
                        checked={elegidas.includes(hoja)}
                        onChange={(e) =>
                          setElegidas((prev) =>
                            e.target.checked
                              ? [...prev, hoja]
                              : prev.filter((h) => h !== hoja),
                          )
                        }
                      />
                      {hoja}
                    </label>
                  </li>
                ))}
              </ul>
              {avisosPreview.map((w, i) => (
                <p key={i} className="mt-2 text-xs text-vektor-ink/70">
                  {w}
                </p>
              ))}
              <Button
                size="sm"
                className="mt-3"
                disabled={aplicando || elegidas.length === 0}
                onClick={() => void aplicar()}
              >
                {aplicando ? "Aplicando…" : "Aplicar las ventas al inventario"}
              </Button>
            </>
          )}
        </div>
      )}

      {resultado && (
        <div className="mt-4 border-t border-vektor-border/50 pt-3 text-xs">
          <p className="text-vektor-ink">
            {resultado.aplicadas === 1
              ? "Se descontó 1 venta del inventario."
              : `Se descontaron ${resultado.aplicadas} ventas del inventario.`}
            {resultado.ya_aplicadas > 0 &&
              ` ${resultado.ya_aplicadas} ya estaban descontadas.`}
          </p>
          {resultado.sin_stock.length > 0 && (
            <div className="mt-2 rounded border border-vk-warning/20 bg-vk-warning-bg px-3 py-2 text-vk-warning">
              <p className="font-medium">
                {resultado.sin_stock.length === 1
                  ? "1 venta quedó sin descontar por falta de stock."
                  : `${resultado.sin_stock.length} ventas quedaron sin descontar por falta de stock.`}
              </p>
              <p className="mt-1">
                No se anularon: siguen registradas como ventas. Cargá el
                inventario que falta y volvé a aplicar — sólo entran las que
                quedaron pendientes.
              </p>
              <ul className="mt-2 list-disc space-y-0.5 pl-4">
                {resultado.sin_stock.slice(0, 5).map((v) => (
                  <li key={v.sale_id}>
                    {v.product_name}: se vendieron {v.quantity} y quedaban{" "}
                    {v.disponible}
                  </li>
                ))}
              </ul>
              {resultado.sin_stock.length > 5 && (
                <p className="mt-1">
                  y {resultado.sin_stock.length - 5} más.
                </p>
              )}
            </div>
          )}
          {resultado.warnings.map((w, i) => (
            <p key={i} className="mt-2 text-vektor-ink/70">
              {w}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}
