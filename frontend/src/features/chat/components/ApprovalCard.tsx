"use client";

interface ApprovalCardProps {
  summary: string;
  pendingActionId: string;
  onConfirm: (id: string) => void;
  onCancel: (id: string) => void;
}

export function ApprovalCard({
  summary,
  pendingActionId,
  onConfirm,
  onCancel,
}: ApprovalCardProps) {
  return (
    <div className="border border-amber-200 bg-amber-50 rounded-lg p-3 my-2">
      <p className="text-sm text-vk-text-primary mb-3 whitespace-pre-wrap">{summary}</p>
      <div className="flex gap-2">
        <button
          onClick={() => onConfirm(pendingActionId)}
          className="flex-1 bg-vk-blue text-white text-sm py-1.5 rounded-md hover:bg-vk-blue-hover transition-colors"
        >
          Confirmar
        </button>
        <button
          onClick={() => onCancel(pendingActionId)}
          className="flex-1 bg-vk-border-w text-vk-text-secondary text-sm py-1.5 rounded-md hover:bg-vk-border-w-hover transition-colors"
        >
          Cancelar
        </button>
      </div>
    </div>
  );
}
