"use client";

import { useState } from "react";
import { Download } from "lucide-react";
import { downloadBlob } from "@/lib/csv";
import { entityExportService } from "@/services/entityExport.service";
import type { AvailableFieldEntityType } from "@/services/fieldCatalog.service";
import { useToastStore } from "@/stores/toastStore";

/**
 * "Exportar todos los campos" — Cambio 3 del plan de conservación de datos.
 * Distinto del "Exportar CSV" que ya trae `SmartTable` (columnas visibles,
 * filas cargadas): esto pide al servidor TODO — todos los campos
 * autorizados, todas las filas del alcance — sin el techo de acumulación del
 * frontend. Componente COMPARTIDO: mismo botón para las 5 secciones.
 */
interface Props {
  entityType: AvailableFieldEntityType;
  /** Para el nombre de archivo y el mensaje de error (ej. "Productos"). */
  entityLabel: string;
  includeInactive?: boolean;
}

export function ExportAllFieldsButton({ entityType, entityLabel, includeInactive = false }: Props) {
  const [loading, setLoading] = useState(false);
  const toast = useToastStore((s) => s.add);

  const handleClick = async () => {
    setLoading(true);
    try {
      const blob = await entityExportService.exportAll(entityType, includeInactive);
      const fecha = new Date().toISOString().slice(0, 10);
      downloadBlob(`vektor-${entityType}-completo-${fecha}.csv`, blob);
    } catch {
      toast(`No se pudo exportar ${entityLabel}. Probá de nuevo.`, "error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <button
      type="button"
      onClick={() => void handleClick()}
      disabled={loading}
      title="Exporta todos los campos y todos los registros del alcance, aunque estén ocultos o no cargados en esta pantalla"
      className="flex items-center gap-1.5 rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-xs text-vk-text-secondary hover:text-vk-text-primary transition-colors disabled:opacity-50"
    >
      <Download className="h-3.5 w-3.5" />
      {loading ? "Exportando…" : "Exportar todo"}
    </button>
  );
}
