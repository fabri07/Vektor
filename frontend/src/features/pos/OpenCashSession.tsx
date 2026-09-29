"use client";

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { toCents } from "@/lib/pos/cart";

interface OpenCashSessionProps {
  onAbrir: (fondoCents: number) => Promise<void>;
  /** Turnos abiertos anteriores de este usuario en esta caja. */
  anterioresAbiertos: number;
  /** Por qué se cerró el turno anterior, si lo cerró otro. */
  aviso?: string | null;
}

/**
 * Abrir el turno antes de cobrar (B8).
 *
 * El fondo lo declara quien abre: el servidor no lo puede saber. `$0` es un
 * fondo válido y hay que escribirlo: un campo vacío no es "cero", es "no lo
 * dije". Se puede abrir sin red; el turno se avisa solo cuando vuelve.
 */
export function OpenCashSession({ onAbrir, anterioresAbiertos, aviso }: OpenCashSessionProps) {
  const [fondo, setFondo] = useState("");
  const [abriendo, setAbriendo] = useState(false);
  const cents = fondo.trim() === "" ? null : toCents(fondo);

  async function abrir(): Promise<void> {
    if (cents === null) return;
    setAbriendo(true);
    try {
      await onAbrir(cents);
    } finally {
      setAbriendo(false);
    }
  }

  return (
    <div className="flex flex-1 items-center justify-center p-6">
      <form
        className="w-full max-w-sm space-y-4 rounded-xl border border-pos-border bg-pos-surface p-6"
        onSubmit={(e) => {
          e.preventDefault();
          void abrir();
        }}
      >
        <h2 className="text-lg font-semibold text-pos-text">Abrir turno</h2>
        {aviso ? (
          <p className="text-sm text-pos-danger" role="alert" data-testid="aviso-turno-cerrado">
            {aviso}
          </p>
        ) : null}
        <p className="text-sm text-pos-text-2">
          Contá el efectivo que hay en el cajón antes de empezar. Al cerrar el turno se compara
          contra este fondo más lo que cobres en efectivo.
        </p>
        <label className="block space-y-1 text-sm text-pos-text">
          <span>Fondo inicial en el cajón</span>
          <input
            aria-label="Fondo inicial"
            inputMode="decimal"
            autoFocus
            data-pos-scan="capture"
            placeholder="0,00"
            value={fondo}
            onChange={(e) => setFondo(e.target.value)}
            className="w-full rounded-lg border border-pos-border bg-pos-field px-3 py-2 text-right text-lg text-pos-text"
          />
        </label>
        {fondo.trim() !== "" && cents === null ? (
          <p className="text-sm text-pos-danger">Escribí un monto válido, por ejemplo 5000 o 5000,50.</p>
        ) : null}
        {anterioresAbiertos > 0 ? (
          <p className="text-sm text-vk-warning" role="status">
            Tenés {anterioresAbiertos} {anterioresAbiertos === 1 ? "turno anterior" : "turnos anteriores"}{" "}
            sin cerrar en esta caja. Pedíselos al encargado.
          </p>
        ) : null}
        <Button type="submit" className="w-full" size="lg" loading={abriendo} disabled={cents === null}>
          Abrir turno
        </Button>
      </form>
    </div>
  );
}
