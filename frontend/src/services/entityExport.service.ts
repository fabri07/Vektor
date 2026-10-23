import { api } from "@/lib/api";
import type { AvailableFieldEntityType } from "@/services/fieldCatalog.service";

/**
 * Cambio 3 del plan de conservación de datos — "Exportar todos los campos":
 * distinto del CSV que ya arma `SmartTable` (columnas visibles, filas
 * cargadas). Este pega contra `GET /export/{entity_type}`, que trae TODO del
 * lado servidor, paginado puertas adentro — sin el techo de acumulación del
 * frontend.
 */
export const entityExportService = {
  async exportAll(
    entityType: AvailableFieldEntityType,
    includeInactive = false,
  ): Promise<Blob> {
    const res = await api.get(`/export/${entityType}`, {
      params: { include_inactive: includeInactive },
      responseType: "blob",
    });
    return res.data as Blob;
  },
};
