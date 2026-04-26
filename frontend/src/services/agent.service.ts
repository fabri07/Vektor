import { api } from "@/lib/api";
import type { AutomationOffer } from "@/services/automations.service";

export interface AgentResponse {
  request_id: string;
  agent_name: string;
  status: "success" | "requires_approval" | "requires_clarification" | "error" | "requires_google_auth";
  risk_level: string;
  requires_approval: boolean;
  result: {
    summary?: string;
    health_score?: number;
    alerts?: unknown[];
    message?: string;
    auth_url?: string;
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

export interface ConfirmActionResponse {
  status: string;
  action_type: string;
  execution_status: string;
  failure_code?: string | null;
  automation_offer?: AutomationOffer;
}

export async function sendMessage(
  message: string,
  conversationId?: string,
  attachments?: ChatAttachment[],
): Promise<AgentResponse> {
  const res = await api.post<AgentResponse>(
    "/agent/chat",
    {
      message,
      conversation_id: conversationId,
      attachments: attachments ?? [],
    },
    { timeout: 90_000 },
  );
  return res.data;
}

export async function confirmAction(pendingActionId: string): Promise<ConfirmActionResponse> {
  const res = await api.post<ConfirmActionResponse>(`/agent/confirm/${pendingActionId}`);
  return res.data;
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
