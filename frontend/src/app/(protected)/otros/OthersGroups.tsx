"use client";

import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import type { AxiosError } from "axios";
import { Modal } from "@/components/ui/Modal";
import {
  othersService,
  type DismissGroupPreview,
  type OthersGroup,
} from "@/services/others.service";
import { useToastStore } from "@/stores/toastStore";

const ENTITY: Record<string, string> = {
  sale: "Venta",
  expense: "Gasto",
  product: "Producto",
};

/**
 * B·5a: la bandeja agrupada por archivo × hoja × motivo × sugerencia. Cada grupo
 * se puede ver (filtra la tabla) o descartar entero. El descarte pasa siempre
 * por una vista previa: se descarta exactamente lo que se vio, y si el grupo
 * cambió en el medio el backend responde 409 y se vuelve a mostrar.
 */
export function OthersGroups({
  selected,
  onSelect,
  onDismissed,
}: {
  selected: string | null;
  onSelect: (group: OthersGroup | null) => void;
  onDismissed: () => Promise<void>;
}) {
  const toast = useToastStore((s) => s.add);
  const [discarding, setDiscarding] = useState<OthersGroup | null>(null);
  const [preview, setPreview] = useState<DismissGroupPreview | null>(null);

  const { data: groups = [] } = useQuery({
    queryKey: ["others-summary"],
    queryFn: () => othersService.getSummary(),
    staleTime: 60 * 1000,
  });

  const previewMutation = useMutation({
    mutationFn: (g: OthersGroup) => othersService.previewDismissGroup(g.group_key),
    onSuccess: (p) => setPreview(p),
    onError: () => toast("No se pudo preparar el descarte.", "error"),
  });

  const dismissMutation = useMutation({
    mutationFn: ({ g, token }: { g: OthersGroup; token: string }) =>
      othersService.dismissGroup(g.group_key, token),
    onSuccess: async (res) => {
      toast(`${res.dismissed} registro(s) descartados.`, "success");
      setDiscarding(null);
      setPreview(null);
      if (selected === discarding?.group_key) onSelect(null);
      await onDismissed();
    },
    onError: (error, vars) => {
      const code = (error as AxiosError<{ detail?: { code?: string } }>)?.response?.data
        ?.detail?.code;
      if (code === "GROUP_CHANGED") {
        toast("El grupo cambió mientras lo revisabas. Te mostramos cómo quedó.", "info");
        previewMutation.mutate(vars.g);
      } else {
        toast("No se pudo descartar el grupo.", "error");
      }
    },
  });

  if (groups.length <= 1) return null;

  const abrir = (g: OthersGroup) => {
    setDiscarding(g);
    setPreview(null);
    previewMutation.mutate(g);
  };

  return (
    <section className="rounded-lg border border-vk-border-w bg-vk-surface-w p-3">
      <h2 className="mb-2 text-sm font-semibold text-vk-text-primary">Por archivo y motivo</h2>
      <ul className="space-y-1.5">
        {groups.map((g) => (
          <li
            key={g.group_key}
            className={`flex flex-wrap items-center justify-between gap-2 rounded px-2 py-1.5 text-sm ${
              selected === g.group_key ? "bg-vk-bg-light" : ""
            }`}
          >
            <span className="min-w-0 flex-1">
              <span className="font-medium text-vk-text-primary">
                {g.file_name ?? "Sin archivo"}
              </span>
              <span className="text-vk-text-muted">
                {" · "}
                {g.motivo ?? "Sin motivo"}
                {g.suggested_entity ? ` · ${ENTITY[g.suggested_entity] ?? g.suggested_entity}` : ""}
              </span>
            </span>
            <span className="flex items-center gap-1.5">
              <span className="text-xs text-vk-text-muted">{g.count}</span>
              <button
                type="button"
                onClick={() => onSelect(selected === g.group_key ? null : g)}
                className="rounded border border-vk-border-w px-2 py-1 text-xs text-vk-text-primary hover:bg-vk-bg-light"
              >
                {selected === g.group_key ? "Ver todos" : "Ver"}
              </button>
              <button
                type="button"
                onClick={() => abrir(g)}
                className="rounded border border-vk-danger/30 px-2 py-1 text-xs text-vk-danger hover:bg-vk-danger-bg"
              >
                Descartar grupo
              </button>
            </span>
          </li>
        ))}
      </ul>

      <Modal
        isOpen={discarding !== null}
        onClose={() => {
          setDiscarding(null);
          setPreview(null);
        }}
        title="Descartar el grupo"
        size="lg"
      >
        {!preview ? (
          <p className="text-sm text-vk-text-muted">Preparando la vista previa…</p>
        ) : (
          <div className="space-y-3 text-sm">
            <p className="text-vk-text-primary">
              Se van a descartar <strong>{preview.count}</strong> registro(s) de{" "}
              <strong>{discarding?.file_name ?? "sin archivo"}</strong> ({discarding?.motivo ?? "sin motivo"}).
              No se importan a ninguna sección.
            </p>
            {preview.samples.length > 0 && (
              <ul className="space-y-1 rounded border border-vk-border-w p-2 text-xs text-vk-text-muted">
                {preview.samples.map((s, i) => (
                  <li key={i} className="truncate">
                    {Object.values(s).filter(Boolean).join(" · ")}
                  </li>
                ))}
              </ul>
            )}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  setDiscarding(null);
                  setPreview(null);
                }}
                className="rounded border border-vk-border-w px-3 py-1.5 text-vk-text-primary"
              >
                Cancelar
              </button>
              <button
                type="button"
                disabled={dismissMutation.isPending || preview.count === 0}
                onClick={() =>
                  discarding && dismissMutation.mutate({ g: discarding, token: preview.group_token })
                }
                className="rounded border border-vk-danger/40 bg-vk-danger-bg px-3 py-1.5 text-vk-danger disabled:opacity-50"
              >
                Descartar {preview.count}
              </button>
            </div>
          </div>
        )}
      </Modal>
    </section>
  );
}
