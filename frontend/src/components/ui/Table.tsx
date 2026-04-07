import type { ReactNode } from "react";

export interface TableColumn<T = Record<string, unknown>> {
  key: string;
  header: string;
  render?: (value: unknown, row: T) => ReactNode;
}

interface TableProps<T = Record<string, unknown>> {
  columns: TableColumn<T>[];
  data: T[];
  emptyMessage?: string;
}

export function Table<T extends object>({
  columns,
  data,
  emptyMessage = "No hay datos para mostrar.",
}: TableProps<T>) {
  return (
    /* Wrapper con scroll horizontal + sombras laterales como indicador de overflow */
    <div className="relative">
      {/* Gradiente izquierdo — visible cuando hay scroll */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-y-0 left-0 z-10 w-6 bg-gradient-to-r from-vk-surface-w to-transparent"
      />
      {/* Gradiente derecho */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-y-0 right-0 z-10 w-6 bg-gradient-to-l from-vk-surface-w to-transparent"
      />

      <div className="overflow-x-auto">
        <table className="w-full min-w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-vk-border-w">
              {columns.map((col) => (
                <th
                  key={col.key}
                  scope="col"
                  className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-vk-text-secondary"
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>

          <tbody>
            {data.length === 0 ? (
              <tr>
                <td
                  colSpan={columns.length}
                  className="px-4 py-8 text-center text-sm text-vk-text-muted"
                >
                  {emptyMessage}
                </td>
              </tr>
            ) : (
              data.map((row, rowIdx) => (
                <tr
                  key={rowIdx}
                  className="border-b border-vk-border-w/60 transition-colors hover:bg-vk-bg-light"
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className="whitespace-nowrap px-4 py-3 text-vk-text-primary"
                    >
                      {col.render
                        ? col.render((row as Record<string, unknown>)[col.key], row)
                        : String((row as Record<string, unknown>)[col.key] ?? "")}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
