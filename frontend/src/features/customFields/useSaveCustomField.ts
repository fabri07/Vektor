import { useCallback } from "react";
import { useQueryClient, type QueryKey } from "@tanstack/react-query";
import { useToastStore } from "@/stores/toastStore";

interface Options {
  /** Prefijo de la(s) query(s) de lista de la sección (ej: ["sales-entries"]). */
  listKey: QueryKey;
  /** PATCH de la entidad: persiste el dict completo de custom_fields. */
  update: (id: string, customFields: Record<string, unknown>) => Promise<unknown>;
  /** Queries adicionales a invalidar tras guardar (ej: dashboards). */
  extraInvalidate?: QueryKey[];
}

/**
 * Handler compartido para guardar el valor de un custom field editado inline.
 *
 * Hace un **update optimista** sobre todas las queries que matchean `listKey`
 * antes del PATCH: así una segunda edición en la misma fila (antes de que llegue
 * el refetch) mergea contra datos frescos en vez de pisar el campo recién
 * guardado. El backend reemplaza el dict completo de custom_fields, por eso el
 * caller ya manda el merge hecho. Centraliza el closure que antes se repetía en
 * las 6 páginas de tabla.
 */
export function useSaveCustomField({ listKey, update, extraInvalidate }: Options) {
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  // `row: unknown` para ser asignable al onSave de cualquier entidad (T) por
  // contravarianza; se castea internamente para leer el id.
  return useCallback(
    async (row: unknown, customFields: Record<string, unknown>) => {
      const rowId = (row as { id?: unknown }).id;
      const id = String(rowId);

      queryClient.setQueriesData<unknown>({ queryKey: listKey }, (old: unknown) => {
        if (!Array.isArray(old)) return old;
        return old.map((r) =>
          r && typeof r === "object" && (r as { id?: unknown }).id === rowId
            ? { ...(r as object), custom_fields: customFields }
            : r,
        );
      });

      try {
        await update(id, customFields);
      } catch {
        toast("No se pudo guardar el dato.", "error");
      } finally {
        await queryClient.invalidateQueries({ queryKey: listKey });
        for (const key of extraInvalidate ?? []) {
          await queryClient.invalidateQueries({ queryKey: key });
        }
      }
    },
    [queryClient, toast, listKey, update, extraInvalidate],
  );
}
