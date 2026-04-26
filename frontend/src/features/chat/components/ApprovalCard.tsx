"use client";

import type { AutomationOffer } from "@/services/automations.service";

interface ApprovalCardProps {
  summary: string;
  pendingActionId: string;
  automationOffer?: AutomationOffer;
  onConfirm: (id: string) => void;
  onCancel: (id: string) => void;
  onAutomate?: (id: string) => void;
  onDismissAutomation?: (id: string) => void;
}

export function ApprovalCard({
  summary,
  pendingActionId,
  automationOffer,
  onConfirm,
  onCancel,
  onAutomate,
  onDismissAutomation,
}: ApprovalCardProps) {
  if (automationOffer) {
    return (
      <div className="my-2 rounded-lg border border-vk-blue/30 bg-vk-bg-light p-3">
        <p className="mb-2 whitespace-pre-wrap text-sm text-vk-text-primary">{summary}</p>
        <p className="mb-3 text-xs leading-5 text-vk-text-secondary">
          {automationOffer.message} Podés desactivarlo después desde Configuración.
        </p>
        <div className="flex gap-2">
          <button
            onClick={() => onAutomate?.(pendingActionId)}
            className="flex-1 rounded-md bg-vk-blue py-1.5 text-sm text-white transition-colors hover:bg-vk-blue-hover"
          >
            Automatizar
          </button>
          <button
            onClick={() => onDismissAutomation?.(pendingActionId)}
            className="flex-1 rounded-md bg-vk-border-w py-1.5 text-sm text-vk-text-secondary transition-colors hover:bg-vk-border-w-hover"
          >
            No automatizar
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="border border-amber-400/30 bg-vk-bg-light rounded-lg p-3 my-2">
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
