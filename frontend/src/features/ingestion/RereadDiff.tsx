"use client";

import type { RereadItem } from "@/services/ingestion.service";

const ACTION_LABELS: Record<string, string> = {
  update: "Actualizado",
  updated: "Actualizado",
  insert: "Nuevo",
  inserted: "Nuevo",
  new: "Nuevo",
  void: "Anulado",
  voided: "Anulado",
  preserve: "Preservado",
  preserved: "Preservado",
};

const ACTION_COLORS: Record<string, string> = {
  update: "text-vk-info bg-vk-info-bg",
  updated: "text-vk-info bg-vk-info-bg",
  insert: "text-vk-success bg-vk-success-bg",
  inserted: "text-vk-success bg-vk-success-bg",
  new: "text-vk-success bg-vk-success-bg",
  void: "text-vk-danger bg-vk-danger-bg",
  voided: "text-vk-danger bg-vk-danger-bg",
  preserve: "text-vk-text-muted bg-vk-border-w",
  preserved: "text-vk-text-muted bg-vk-border-w",
};

function actionLabel(action: string): string {
  return ACTION_LABELS[action.toLowerCase()] ?? action;
}

function actionColor(action: string): string {
  return ACTION_COLORS[action.toLowerCase()] ?? "text-vk-text-muted bg-vk-border-w";
}

function formatValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** Une las claves de before/after preservando un orden estable. */
function unionKeys(
  before: Record<string, unknown> | null,
  after: Record<string, unknown> | null,
): string[] {
  const keys: string[] = [];
  const seen = new Set<string>();
  for (const obj of [before, after]) {
    if (!obj) continue;
    for (const k of Object.keys(obj)) {
      if (!seen.has(k)) {
        seen.add(k);
        keys.push(k);
      }
    }
  }
  return keys;
}

function ItemDiff({ item }: { item: RereadItem }) {
  const { before, after } = item;
  const keys = unionKeys(before, after);

  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-3">
      <div className="mb-2 flex items-center gap-2">
        <span
          className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${actionColor(item.action)}`}
        >
          {actionLabel(item.action)}
        </span>
      </div>

      <div className="grid grid-cols-[minmax(80px,1fr)_1fr_1fr] gap-x-3 gap-y-1 text-xs">
        <div className="font-medium text-vk-text-muted">Campo</div>
        <div className="font-medium text-vk-text-muted">Antes</div>
        <div className="font-medium text-vk-text-muted">Después</div>

        {keys.map((key) => {
          const beforeVal = before?.[key];
          const afterVal = after?.[key];
          const changed = formatValue(beforeVal) !== formatValue(afterVal);
          return (
            <div key={key} className="contents">
              <div className="truncate text-vk-text-secondary">{key}</div>
              <div
                className={`truncate ${changed ? "text-vk-text-secondary line-through opacity-60" : "text-vk-text-muted"}`}
              >
                {formatValue(beforeVal)}
              </div>
              <div
                className={`truncate ${changed ? "font-medium text-vk-text-primary" : "text-vk-text-muted"}`}
              >
                {formatValue(afterVal)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function RereadDiff({ items }: { items: RereadItem[] }) {
  if (items.length === 0) {
    return (
      <p className="text-sm text-vk-text-muted">
        No hubo cambios concretos para mostrar en esta relectura.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {items.map((item, idx) => (
        <ItemDiff key={idx} item={item} />
      ))}
    </div>
  );
}
