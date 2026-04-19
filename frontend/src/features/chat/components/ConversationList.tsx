"use client";

import { Plus } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { getConversations } from "@/services/agent.service";

interface ConversationListProps {
  activeConversationId: string;
  onSelect: (conversationId: string) => void;
  onNew: () => void;
}

function relativeDate(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return "ahora";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

export function ConversationList({ activeConversationId, onSelect, onNew }: ConversationListProps) {
  const { data: conversations = [] } = useQuery({
    queryKey: ["conversations"],
    queryFn: getConversations,
    staleTime: 30_000,
  });

  return (
    <div className="flex h-full flex-col">
      <div className="p-3 border-b border-vk-border-w">
        <button
          onClick={onNew}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-2 text-xs font-medium text-vk-text-secondary hover:bg-vk-bg-light transition-colors"
        >
          <Plus className="h-3.5 w-3.5" />
          Nueva conversación
        </button>
      </div>

      <div className="flex-1 overflow-y-auto scrollbar-thin py-1">
        {conversations.length === 0 ? (
          <p className="px-3 py-4 text-xs text-vk-text-muted text-center">
            Sin conversaciones anteriores
          </p>
        ) : (
          conversations.map((conv) => (
            <button
              key={conv.conversation_id}
              onClick={() => onSelect(conv.conversation_id)}
              className={`w-full text-left px-3 py-2 rounded-lg mx-1 flex items-start justify-between gap-2 transition-colors
                ${
                  conv.conversation_id === activeConversationId
                    ? "bg-vk-blue/10 text-vk-blue"
                    : "text-vk-text-secondary hover:bg-vk-bg-light"
                }`}
              style={{ width: "calc(100% - 8px)" }}
            >
              <span className="text-xs truncate leading-5">{conv.title}</span>
              <span className="text-[10px] shrink-0 text-vk-text-muted mt-0.5">
                {relativeDate(conv.updated_at)}
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}
