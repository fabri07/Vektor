"use client";

import { useQuery } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import {
  fieldCatalogService,
  type AvailableFieldEntityType,
} from "@/services/fieldCatalog.service";
import { buildAllDataRows, type AllDataRow } from "@/lib/fieldCatalog";
import { useAuthStore } from "@/stores/authStore";

/**
 * "Todos los datos" — Cambio 2 del plan de conservación y acceso a datos de
 * negocio (docs/plans/conservacion-y-acceso-datos-negocio.md): detalle
 * COMPARTIDO entre las 5 secciones, accesible desde cada registro. Muestra
 * todo lo que el catálogo de lectura (Cambio 1) declara para la entidad —
 * canónico, evidencia y adicional, oculto en la tabla o no — porque "la
 * tabla puede ocultar un dato; no puede hacerlo desaparecer".
 */

const ORIGIN_LABEL: Record<AllDataRow["origin"], string> = {
  canonical: "Campo del negocio",
  evidence: "Evidencia del archivo importado",
  additional: "Campo adicional",
};

const ORIGIN_BADGE_CLASS: Record<AllDataRow["origin"], string> = {
  canonical: "bg-vk-bg-light text-vk-text-secondary",
  evidence: "bg-vk-warning-bg text-vk-warning",
  additional: "bg-vk-info-bg text-vk-info",
};

interface Props {
  entityType: AvailableFieldEntityType;
  /** Título del modal — normalmente el nombre/identificador del registro. */
  title: string;
  row: object;
  isOpen: boolean;
  onClose: () => void;
}

export function AllDataModal({ entityType, title, row, isOpen, onClose }: Props) {
  const user = useAuthStore((s) => s.user);
  const { data: available = [], isLoading, isError, refetch } = useQuery({
    queryKey: ["fields-available", entityType, user?.tenant_id, user?.id],
    queryFn: () => fieldCatalogService.getAvailableFields(entityType),
    enabled: isOpen,
    staleTime: 5 * 60 * 1000,
  });

  const rows = buildAllDataRows(available, row);

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={title} size="lg">
      <p className="mb-4 text-sm text-vk-text-muted">
        Todos los datos guardados de este registro, estén o no visibles en la tabla.
      </p>
      <p className="mb-4 text-xs text-vk-text-muted">
        La procedencia exacta (archivo, hoja y fila) no está disponible en este detalle.
      </p>
      {isLoading ? (
        <p className="py-6 text-center text-sm text-vk-text-muted">Cargando…</p>
      ) : isError ? (
        <div role="alert" className="py-6 text-center text-sm text-vk-text-muted">
          <p>No se pudo cargar el catálogo de campos. Los datos del registro no se modificaron.</p>
          <button type="button" onClick={() => void refetch()} className="mt-2 underline">Reintentar</button>
        </div>
      ) : rows.length === 0 ? (
        <p className="py-6 text-center text-sm text-vk-text-muted">
          Sin campos declarados para esta sección todavía.
        </p>
      ) : (
        <dl className="max-h-[60vh] divide-y divide-vk-border-w overflow-y-auto">
          {rows.map((r) => (
            <div
              key={r.field_id}
              className="flex flex-col gap-1 py-2.5 sm:flex-row sm:items-start sm:justify-between sm:gap-4"
            >
              <dt className="flex flex-wrap items-center gap-1.5 text-sm text-vk-text-secondary">
                <span>{r.label}</span>
                {r.unit && <span className="text-xs text-vk-text-muted">({r.unit})</span>}
                <span
                  className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${ORIGIN_BADGE_CLASS[r.origin]}`}
                >
                  {ORIGIN_LABEL[r.origin]}
                </span>
                {!r.enabledForNewEntries && (
                  <span className="rounded-full bg-vk-danger-bg px-1.5 py-0.5 text-[10px] font-medium text-vk-danger">
                    No disponible para nuevas cargas
                  </span>
                )}
              </dt>
              <dd className="max-w-full break-words text-sm text-vk-text-primary sm:text-right">
                {r.formatted.length > 400 ? (
                  <details>
                    <summary className="cursor-pointer">Ver valor completo</summary>
                    <p className="whitespace-pre-wrap">{r.formatted}</p>
                  </details>
                ) : <p className="whitespace-pre-wrap">{r.formatted}</p>}
                {r.financialRule && (
                  <p className="text-xs text-vk-text-muted">{r.financialRule}</p>
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </Modal>
  );
}
