"use client";

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { acknowledgeAction } from "@/services/dashboard.service";
import type { ActionSuggestionResponse } from "@/types/api";

interface Props {
  action: ActionSuggestionResponse;
}

export function ActionCard({ action }: Props) {
  const queryClient = useQueryClient();
  const [acknowledged, setAcknowledged] = useState(
    action.status === "acknowledged",
  );

  const { mutate, isPending } = useMutation({
    mutationFn: () => acknowledgeAction(action.id),
    onSuccess: () => {
      setAcknowledged(true);
      queryClient.invalidateQueries({ queryKey: ["insights", "current"] });
    },
  });

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
      <p className="mb-3 text-xs font-medium uppercase tracking-widest text-gray-400">
        Acción Sugerida
      </p>
      <div className="flex items-start gap-2">
        <div className="mt-0.5 flex-shrink-0 rounded-full bg-[#1A1A2E]/8 p-1.5">
          <svg
            className="h-4 w-4 text-[#1A1A2E]"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z"
            />
          </svg>
        </div>
        <p className="text-sm leading-relaxed text-gray-700">{action.description}</p>
      </div>

      <div className="mt-5">
        {acknowledged ? (
          <span className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-600">
            <svg className="h-3.5 w-3.5" fill="currentColor" viewBox="0 0 20 20">
              <path
                fillRule="evenodd"
                d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
                clipRule="evenodd"
              />
            </svg>
            Marcada como vista
          </span>
        ) : (
          <Button
            variant="secondary"
            size="sm"
            loading={isPending}
            onClick={() => mutate()}
          >
            Marcar como vista
          </Button>
        )}
      </div>
    </div>
  );
}
