"use client";

import { useState, useCallback, useRef, useEffect } from "react";
import { Download, SlidersHorizontal, Check } from "lucide-react";
import { Table } from "./Table";
import type { TableColumn } from "./Table";

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
}

function toCSVValue(val: unknown): string {
  const s = val == null ? "" : String(val);
  if (s.includes(",") || s.includes('"') || s.includes("\n")) {
    return `"${s.replace(/"/g, '""')}"`;
  }
  return s;
}

export function SmartTable<T extends object>({
  columns,
  data,
  emptyMessage,
  exportFilename = "vektor-export",
}: SmartTableProps<T>) {
  const [visibleKeys, setVisibleKeys] = useState<Set<string>>(
    () =>
      new Set(
        columns
          .filter((c) => c.defaultVisible !== false)
          .map((c) => c.key),
      ),
  );
  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);

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
  }, []);

  const visibleColumns = columns.filter((c) => visibleKeys.has(c.key));
  const hideableColumns = columns.filter((c) => c.hideable !== false);

  const exportCSV = useCallback(() => {
    const headers = visibleColumns.map((c) => toCSVValue(c.header));
    const rows = data.map((row) =>
      visibleColumns.map((col) => {
        const raw = (row as Record<string, unknown>)[col.key];
        const cell = col.csvValue ? col.csvValue(raw, row) : raw;
        return toCSVValue(cell);
      }),
    );

    const csv = [headers, ...rows].map((r) => r.join(",")).join("\n");
    const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${exportFilename}-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }, [visibleColumns, data, exportFilename]);

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center justify-end gap-2">
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
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {/* Export CSV */}
        <button
          onClick={exportCSV}
          disabled={data.length === 0}
          className="flex items-center gap-1.5 rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-xs text-vk-text-secondary hover:text-vk-text-primary transition-colors disabled:opacity-40"
        >
          <Download className="h-3.5 w-3.5" />
          Exportar CSV
        </button>
      </div>

      <Table columns={visibleColumns} data={data} emptyMessage={emptyMessage} />
    </div>
  );
}
