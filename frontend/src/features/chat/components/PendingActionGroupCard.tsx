"use client";

interface TaskSummary {
  line: string;
}

interface PendingActionGroupCardProps {
  summary: string;
  approvalGroupId: string;
  onConfirmGroup: (groupId: string) => void;
  onCancelGroup: (groupId: string) => void;
}

function parseTaskLines(summary: string): TaskSummary[] {
  return summary
    .split("\n")
    .filter((l) => l.trim().startsWith("•"))
    .map((l) => ({ line: l.trim() }));
}

export function PendingActionGroupCard({
  summary,
  approvalGroupId,
  onConfirmGroup,
  onCancelGroup,
}: PendingActionGroupCardProps) {
  const tasks = parseTaskLines(summary);

  return (
    <div className="my-2 rounded-lg border border-amber-400/30 bg-vk-bg-light p-3">
      <p className="mb-1 text-xs font-medium uppercase tracking-wide text-amber-400">
        Operaciones combinadas
      </p>
      <div className="mb-3 space-y-1">
        {tasks.length > 0 ? (
          tasks.map((t, i) => (
            <p key={i} className="text-sm text-vk-text-primary">
              {t.line}
            </p>
          ))
        ) : (
          <p className="whitespace-pre-wrap text-sm text-vk-text-primary">{summary}</p>
        )}
      </div>
      <div className="flex gap-2">
        <button
          onClick={() => onConfirmGroup(approvalGroupId)}
          className="flex-1 rounded-md bg-vk-blue py-1.5 text-sm text-white transition-colors hover:bg-vk-blue-hover"
        >
          Aprobar todo
        </button>
        <button
          onClick={() => onCancelGroup(approvalGroupId)}
          className="flex-1 rounded-md bg-vk-border-w py-1.5 text-sm text-vk-text-secondary transition-colors hover:bg-vk-border-w-hover"
        >
          Cancelar
        </button>
      </div>
    </div>
  );
}
