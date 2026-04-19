import { api } from "@/lib/api";

export interface AgentResponse {
  request_id: string;
  agent_name: string;
  status: "success" | "requires_approval" | "requires_clarification" | "error";
  risk_level: string;
  requires_approval: boolean;
  result: {
    summary?: string;
    health_score?: number;
    alerts?: unknown[];
    [key: string]: unknown;
  };
  pending_action_id?: string;
  question?: string;
  message?: string;
}

export interface ChatAttachment {
  file_id: string;
  filename: string;
}

export async function sendMessage(
  message: string,
  conversationId?: string,
  attachments?: ChatAttachment[],
): Promise<AgentResponse> {
  const res = await api.post<AgentResponse>("/agent/chat", {
    message,
    conversation_id: conversationId,
    attachments: attachments ?? [],
  });
  return res.data;
}

export async function confirmAction(pendingActionId: string): Promise<void> {
  await api.post(`/agent/confirm/${pendingActionId}`);
}

export async function cancelAction(pendingActionId: string): Promise<void> {
  await api.post(`/agent/cancel/${pendingActionId}`);
}

export async function getChatUsage(): Promise<{ messages_today: number; limit: number }> {
  const res = await api.get<{ messages_today: number; limit: number }>("/agent/usage");
  return res.data;
}

export interface ConversationSummary {
  conversation_id: string;
  title: string;
  updated_at: string;
}

export interface ConversationTurns {
  conversation_id: string;
  turns: Array<{ role: string; content: string }>;
}

export async function getConversations(): Promise<ConversationSummary[]> {
  const res = await api.get<ConversationSummary[]>("/agent/conversations");
  return res.data;
}

export async function getConversation(id: string): Promise<ConversationTurns> {
  const res = await api.get<ConversationTurns>(`/agent/conversations/${id}`);
  return res.data;
}
