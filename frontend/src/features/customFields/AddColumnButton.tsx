"use client";

import { useState } from "react";
import { Columns3 } from "lucide-react";
import { CustomFieldsModal } from "./CustomFieldsModal";

interface Props {
  entityType: string;
  entityLabel: string;
  /** Refresca la tabla tras crear/quitar columnas (ej: invalidar la query de la sección). */
  onChanged?: () => void;
}

/** Botón "Columna" para la toolbar de SmartTable: abre el modal de campos
 * personalizados (crear / quitar / deshacer) de la entidad. */
export function AddColumnButton({ entityType, entityLabel, onChanged }: Props) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex items-center gap-1.5 rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-xs text-vk-text-secondary transition-colors hover:text-vk-text-primary"
      >
        <Columns3 className="h-3.5 w-3.5" />
        Agregar columna
      </button>
      {open && (
        <CustomFieldsModal
          entityType={entityType}
          entityLabel={entityLabel}
          onClose={() => setOpen(false)}
          onChanged={() => onChanged?.()}
        />
      )}
    </>
  );
}
