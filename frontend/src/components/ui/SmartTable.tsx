"use client";

import { useState, useCallback, useRef, useEffect, useMemo, useDeferredValue } from "react";
import { Download, SlidersHorizontal, Check, ChevronLeft, ChevronRight, RotateCcw } from "lucide-react";
import type { ReactNode } from "react";
import { Table } from "./Table";
import type { TableColumn } from "./Table";
import { TableSearch } from "./TableSearch";
import { matchesRow } from "@/lib/search";
import { downloadCSV } from "@/lib/csv";
import {
  borrarPreferenciasDeColumnas,
  guardarPreferenciasDeColumnas,
  leerPreferenciasDeColumnas,
} from "@/lib/columnPreferences";

export interface SmartColumn<T = Record<string, unknown>> extends TableColumn<T> {
  hideable?: boolean;
  defaultVisible?: boolean;
  csvValue?: (value: unknown, row: T) => string;
}

interface SmartTableProps<T extends object> {
  columns: SmartColumn<T>[];
  data: T[];
  emptyMessage?: string;
  exportFilename?: string;
  renderActions?: (row: T) => ReactNode;
  /** Acciones extra a la izquierda de la toolbar (ej: "+ Columna"). */
  toolbarActions?: ReactNode;
  /** Muestra el buscador en la toolbar (default true). */
  searchable?: boolean;
  searchPlaceholder?: string;
  /**
   * Clave para persistir mostrar/ocultar columnas en `localStorage` (Cambio 2
   * del plan de conservación). El caller arma la clave versionada por
   * tenant+usuario+sección (ej. `${tenantId}:${userId}:products`) — esta
   * tabla no conoce identidad, solo persiste bajo la clave que le pasan. Sin
   * esta prop, el comportamiento es el de siempre (en memoria, sin persistir).
   */
  storageKey?: string;
}

type PageSize = number | "all";

const PAGE_SIZE_OPTIONS: { value: PageSize; label: string }[] = [
  { value: 25, label: "25" },
  { value: 50, label: "50" },
  { value: 100, label: "100" },
  { value: "all", label: "Todos" },
];

const DEFAULT_PAGE_SIZE: PageSize = 25;

function defaultVisibleKeys<T>(columns: SmartColumn<T>[]): Set<string> {
  return new Set(columns.filter((c) => c.defaultVisible !== false).map((c) => c.key));
}

export function SmartTable<T extends object>({
  columns,
  data,
  emptyMessage,
  exportFilename = "vektor-export",
  renderActions,
  toolbarActions,
  searchable = true,
  searchPlaceholder,
  storageKey,
}: SmartTableProps<T>) {
  // Preferencias guardadas de ESTA clave (si hay) — se leen una sola vez al
  // montar. Cambiar de storageKey (ej. cambiar de tenant/usuario) remonta el
  // componente en la práctica porque cambia la key de React del caller; leer
  // en cada render sería más "correcto" pero también más caro sin necesidad.
  const preferenciasIniciales = useRef(
    storageKey ? leerPreferenciasDeColumnas(storageKey) : null,
  ).current;

  const [visibleKeys, setVisibleKeys] = useState<Set<string>>(() => {
    if (!preferenciasIniciales) return defaultVisibleKeys(columns);
    // Una columna GUARDADA visible sigue visible aunque ya no exista en el
    // array actual — no le hace daño a nadie y evita que un remount pierda la
    // preferencia por una carrera de timing en las columnas del caller.
    const visibles = new Set(preferenciasIniciales.visible);
    // Columnas que el caller declara HOY pero la preferencia nunca vio
    // (nuevas de verdad, no solo "primera vez que corre esta sesión"):
    // arrancan según su propio defaultVisible, como si no hubiera storage.
    const known = new Set(preferenciasIniciales.known);
    for (const c of columns) {
      if (!known.has(c.key) && c.defaultVisible !== false) visibles.add(c.key);
    }
    return visibles;
  });
  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);
  const [pageSize, setPageSize] = useState<PageSize>(DEFAULT_PAGE_SIZE);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);

  // Columnas que ya vimos: las nuevas (ej: una columna personalizada recién
  // creada con defaultVisible:true, o un campo que una importación acaba de
  // declarar) se agregan a visibleKeys sin pisar los toggles manuales del
  // usuario sobre columnas ya conocidas, y se marcan "Nueva" en el selector
  // hasta la próxima vez que el usuario interactúe con la tabla.
  const knownKeysRef = useRef<Set<string>>(
    new Set([...(preferenciasIniciales?.known ?? []), ...columns.map((c) => c.key)]),
  );
  const [nuevas, setNuevas] = useState<Set<string>>(() => {
    if (!preferenciasIniciales) return new Set();
    const known = new Set(preferenciasIniciales.known);
    return new Set(columns.filter((c) => !known.has(c.key)).map((c) => c.key));
  });
  const columnsSig = columns.map((c) => c.key).join("|");
  useEffect(() => {
    const fresh = columns.filter((c) => !knownKeysRef.current.has(c.key));
    if (fresh.length === 0) return;
    fresh.forEach((c) => knownKeysRef.current.add(c.key));
    setNuevas((prev) => new Set([...prev, ...fresh.map((c) => c.key)]));
    const toShow = fresh.filter((c) => c.defaultVisible !== false).map((c) => c.key);
    if (toShow.length > 0) {
      setVisibleKeys((prev) => new Set([...prev, ...toShow]));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [columnsSig]);

  // Persistir: cualquier cambio de visibilidad, o cualquier columna nueva que
  // se suma a "ya vista", se escribe bajo la misma clave. Sin `storageKey` es
  // un no-op (guardarPreferenciasDeColumnas mira `typeof window` igual, pero
  // acá ni siquiera se llama). `skipNextPersistRef` evita la carrera de
  // "Restablecer": ese cambio de estado SÍ dispara este efecto (el Set es una
  // instancia nueva), y sin la bandera reescribiría en storage el mismo
  // default que se acaba de borrar — quedaría "reseteado" en memoria pero
  // persistido igual, así que la próxima carga de página no vería el reset.
  const skipNextPersistRef = useRef(false);
  useEffect(() => {
    if (!storageKey) return;
    if (skipNextPersistRef.current) {
      skipNextPersistRef.current = false;
      return;
    }
    guardarPreferenciasDeColumnas(storageKey, {
      visible: [...visibleKeys],
      known: [...knownKeysRef.current],
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storageKey, visibleKeys, columnsSig]);

  const resetToDefaults = useCallback(() => {
    skipNextPersistRef.current = true;
    setVisibleKeys(defaultVisibleKeys(columns));
    knownKeysRef.current = new Set(columns.map((c) => c.key));
    setNuevas(new Set());
    if (storageKey) borrarPreferenciasDeColumnas(storageKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [columnsSig, storageKey]);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setPickerOpen(false);
      }
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const toggleColumn = useCallback((key: string) => {
    setVisibleKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) {
        if (next.size > 1) next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
    // Tocarla es "verla": deja de mostrarse como Nueva en el selector.
    setNuevas((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  }, []);

  const visibleColumns = columns.filter((c) => visibleKeys.has(c.key));
  const tableColumns = renderActions
    ? [
        ...visibleColumns,
        {
          key: "__actions",
          header: "Acciones",
          render: (_: unknown, row: T) => renderActions(row),
        },
      ]
    : visibleColumns;
  const hideableColumns = columns.filter((c) => c.hideable !== false);

  // Búsqueda client-side: recorre TODAS las columnas originales (incluidas las
  // ocultas), no solo las visibles. La columna sintética "__actions" no está en
  // `columns`, así que queda naturalmente excluida. Contrato: una columna sin
  // `csvValue` cuya `key` no sea propiedad real de la fila (key sintética) aporta
  // "" → para que su valor sea buscable, definí `csvValue` en esa columna.
  // Depende de `columnsSig` (firma estable de keys), NO de `columns`: los call
  // sites pasan `columns` inline (identidad nueva cada render) y eso anularía el memo.
  const filteredData = useMemo(() => {
    if (!deferredSearch.trim()) return data;
    return data.filter((row) =>
      matchesRow(
        columns.map((col) => {
          const raw = (row as Record<string, unknown>)[col.key];
          return col.csvValue ? col.csvValue(raw, row) : raw;
        }),
        deferredSearch,
      ),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, deferredSearch, columnsSig]);

  // Volver a la primera página cuando cambia la búsqueda.
  useEffect(() => {
    setPage(0);
  }, [deferredSearch]);

  // Paginación client-side: el clamp con safePage evita páginas fuera de rango
  // cuando los datos se achican (filtros, cambio de período) sin necesitar effects.
  const total = filteredData.length;
  const pageCount = pageSize === "all" ? 1 : Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const pageData =
    pageSize === "all" ? filteredData : filteredData.slice(safePage * pageSize, (safePage + 1) * pageSize);
  const rangeStart = total === 0 ? 0 : pageSize === "all" ? 1 : safePage * pageSize + 1;
  const rangeEnd = pageSize === "all" ? total : Math.min((safePage + 1) * pageSize, total);

  const exportCSV = useCallback(() => {
    // downloadCSV escapa cada valor (toCSVValue) y le agrega la fecha al filename;
    // acá solo coerción a string preservando null → "" (no "null").
    const headers = visibleColumns.map((c) => String(c.header ?? ""));
    const rows = filteredData.map((row) =>
      visibleColumns.map((col) => {
        const raw = (row as Record<string, unknown>)[col.key];
        const cell = col.csvValue ? col.csvValue(raw, row) : raw;
        return cell == null ? "" : String(cell);
      }),
    );
    downloadCSV(exportFilename, headers, rows);
  }, [visibleColumns, filteredData, exportFilename]);

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-1 items-center gap-2">
          {searchable && (
            <TableSearch
              value={search}
              onChange={setSearch}
              placeholder={searchPlaceholder}
              className="w-full sm:w-64"
            />
          )}
          {toolbarActions}
        </div>
        <div className="flex items-center gap-2">
        {/* Column picker */}
        {hideableColumns.length > 1 && (
          <div className="relative" ref={pickerRef}>
            <button
              onClick={() => setPickerOpen((o) => !o)}
              className="flex items-center gap-1.5 rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-xs text-vk-text-secondary hover:text-vk-text-primary transition-colors"
            >
              <SlidersHorizontal className="h-3.5 w-3.5" />
              Columnas
            </button>
            {pickerOpen && (
              <div className="absolute right-0 top-9 z-30 w-48 rounded-xl border border-vk-border-w bg-vk-surface-w shadow-lg">
                <p className="border-b border-vk-border-w px-3 py-2 text-xs font-semibold text-vk-text-secondary">
                  Mostrar columnas
                </p>
                <ul className="py-1">
                  {hideableColumns.map((col) => (
                    <li key={col.key}>
                      <button
                        onClick={() => toggleColumn(col.key)}
                        className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm hover:bg-vk-bg-light transition-colors"
                      >
                        <span
                          className={`flex h-4 w-4 items-center justify-center rounded border transition-colors ${
                            visibleKeys.has(col.key)
                              ? "border-vk-blue bg-vk-blue text-white"
                              : "border-vk-border-w"
                          }`}
                        >
                          {visibleKeys.has(col.key) && <Check className="h-2.5 w-2.5" />}
                        </span>
                        <span className="text-vk-text-primary">{col.header}</span>
                        {nuevas.has(col.key) && (
                          <span className="ml-auto rounded-full bg-vk-info-bg px-1.5 py-0.5 text-[10px] font-medium text-vk-info">
                            Nueva
                          </span>
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
                {storageKey && (
                  <button
                    onClick={resetToDefaults}
                    className="flex w-full items-center gap-1.5 border-t border-vk-border-w px-3 py-2 text-left text-xs text-vk-text-secondary hover:bg-vk-bg-light hover:text-vk-text-primary transition-colors"
                  >
                    <RotateCcw className="h-3 w-3" />
                    Restablecer columnas
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {/* Export CSV */}
        <button
          onClick={exportCSV}
          disabled={filteredData.length === 0}
          className="flex items-center gap-1.5 rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-xs text-vk-text-secondary hover:text-vk-text-primary transition-colors disabled:opacity-40"
        >
          <Download className="h-3.5 w-3.5" />
          Exportar CSV
        </button>
        </div>
      </div>

      <Table
        columns={tableColumns}
        data={pageData}
        emptyMessage={
          deferredSearch.trim() ? `Sin resultados para "${deferredSearch.trim()}"` : emptyMessage
        }
      />

      {/* Paginación */}
      {total > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-vk-text-muted">
            Mostrando {rangeStart}–{rangeEnd} de {total}
          </p>
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-1.5 text-xs text-vk-text-muted">
              Filas por página:
              <select
                value={String(pageSize)}
                onChange={(e) => {
                  const v = e.target.value;
                  setPageSize(v === "all" ? "all" : Number(v));
                  setPage(0);
                }}
                className="rounded-lg border border-vk-border-w bg-vk-surface-w px-2 py-1.5 text-xs text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
              >
                {PAGE_SIZE_OPTIONS.map((opt) => (
                  <option key={String(opt.value)} value={String(opt.value)}>
                    {opt.label}
                  </option>
                ))}
              </select>
            </label>
            {pageCount > 1 && (
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  aria-label="Página anterior"
                  disabled={safePage === 0}
                  onClick={() => setPage(safePage - 1)}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-lg border border-vk-border-w bg-vk-surface-w text-vk-text-secondary transition-colors hover:text-vk-text-primary disabled:opacity-40"
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                </button>
                <span className="px-1 text-xs text-vk-text-muted">
                  {safePage + 1} / {pageCount}
                </span>
                <button
                  type="button"
                  aria-label="Página siguiente"
                  disabled={safePage >= pageCount - 1}
                  onClick={() => setPage(safePage + 1)}
                  className="inline-flex h-7 w-7 items-center justify-center rounded-lg border border-vk-border-w bg-vk-surface-w text-vk-text-secondary transition-colors hover:text-vk-text-primary disabled:opacity-40"
                >
                  <ChevronRight className="h-3.5 w-3.5" />
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
