"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { posService, type PosTerminal } from "@/services/pos.service";
import { useAuthStore } from "@/stores/authStore";
import { usePosTerminalStore } from "@/stores/posTerminalStore";
import { useToastStore } from "@/stores/toastStore";

function cuando(iso: string | null): string {
  if (!iso) return "nunca usada";
  return `último uso ${new Date(iso).toLocaleString("es-AR")}`;
}

/**
 * Cajas habilitadas (B6). Sólo el dueño.
 *
 * "Habilitar esta PC" guarda la credencial EN ESTE navegador: la caja es la PC
 * donde se apretó el botón. El secreto no se vuelve a ver — si se pierde (se
 * borraron los datos del navegador), se da de baja y se habilita de nuevo.
 */
export function TerminalsSection() {
  const toast = useToastStore((s) => s.add);
  const queryClient = useQueryClient();
  const tenantId = useAuthStore((s) => s.user?.tenant_id);
  const local = usePosTerminalStore((s) => s.terminal);
  const invalida = usePosTerminalStore((s) => s.invalid);
  const setTerminal = usePosTerminalStore((s) => s.setTerminal);
  const clear = usePosTerminalStore((s) => s.clear);
  const [show, setShow] = useState(false);
  const [nombre, setNombre] = useState("");

  const esEstaPc = (t: PosTerminal) => local?.terminalId === t.id;

  const { data: cajas = [], isLoading } = useQuery({
    queryKey: ["pos-terminals"],
    queryFn: () => posService.listTerminals(),
    enabled: show, // Pide PIN de dueño: no se dispara hasta que lo abre.
  });

  const habilitar = useMutation({
    mutationFn: () => posService.enrollTerminal(nombre.trim()),
    onSuccess: async (t) => {
      if (tenantId) {
        setTerminal({ terminalId: t.id, name: t.name, secret: t.secret, tenantId });
      }
      setNombre("");
      toast(`Esta PC quedó habilitada como "${t.name}".`, "success");
      await queryClient.invalidateQueries({ queryKey: ["pos-terminals"] });
    },
    onError: () => toast("No se pudo habilitar la caja (¿nombre repetido?).", "error"),
  });

  const baja = useMutation({
    mutationFn: (id: string) => posService.disableTerminal(id),
    onSuccess: async (t) => {
      if (local?.terminalId === t.id) clear();
      toast("Caja dada de baja.", "success");
      await queryClient.invalidateQueries({ queryKey: ["pos-terminals"] });
    },
    onError: () => toast("No se pudo dar de baja la caja.", "error"),
  });

  return (
    <section className="rounded-xl border border-vk-border-w bg-vk-surface-w p-5">
      <h3 className="text-sm font-semibold text-vk-text-primary">Cajas habilitadas</h3>
      <p className="mt-1 text-xs text-vk-text-secondary">
        Un cajero sólo puede cobrar desde una PC habilitada acá.
        {local && !invalida && ` Esta PC es la caja "${local.name}".`}
      </p>
      {invalida && (
        <p className="mt-2 text-xs text-vk-danger">
          Esta PC fue dada de baja como caja: habilitala de nuevo para cobrar desde acá.
        </p>
      )}

      {!show ? (
        <Button size="sm" variant="secondary" className="mt-4" onClick={() => setShow(true)}>
          Administrar cajas
        </Button>
      ) : isLoading ? (
        <p className="mt-4 text-sm text-vk-text-muted">Cargando…</p>
      ) : (
        <>
          {cajas.length === 0 ? (
            <p className="mt-4 text-sm text-vk-text-muted">Todavía no hay cajas habilitadas.</p>
          ) : (
            <ul className="mt-4 divide-y divide-vk-border-w">
              {cajas.map((t) => (
                <li key={t.id} className="flex items-center justify-between gap-3 py-3">
                  <div>
                    <p className="text-sm text-vk-text-primary">
                      {t.name}
                      {esEstaPc(t) && " · esta PC"}
                    </p>
                    <p className="text-xs text-vk-text-muted">
                      {t.disabled_at ? "dada de baja" : cuando(t.last_seen_at)}
                    </p>
                  </div>
                  {!t.disabled_at && (
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={baja.isPending}
                      onClick={() => {
                        if (window.confirm(`¿Dar de baja la caja "${t.name}"?`)) baja.mutate(t.id);
                      }}
                    >
                      Dar de baja
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
          {(!local || invalida) && (
            <form
              className="mt-4 flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                if (nombre.trim()) habilitar.mutate();
              }}
            >
              <input
                className="flex-1 rounded border border-vk-border-w px-3 py-2 text-sm"
                placeholder="Nombre de esta caja (ej. Mostrador)"
                value={nombre}
                maxLength={80}
                onChange={(e) => setNombre(e.target.value)}
              />
              <Button type="submit" size="sm" disabled={!nombre.trim() || habilitar.isPending}>
                Habilitar esta PC
              </Button>
            </form>
          )}
        </>
      )}
    </section>
  );
}
