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
  pending_action_ids?: string[];    // Stage 3: grupo multi-task
  approval_group_id?: string;       // Stage 3: vincula PAs del grupo
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
  whatsapp?: {
    url: string;
    body: string;
    to: string;
    channel: string;
  };
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

export async function sendHelpMessage(
  message: string,
  conversationId?: string,
): Promise<AgentResponse> {
  const res = await api.post<AgentResponse>(
    "/agent/help/chat",
    { message, conversation_id: conversationId },
    { timeout: 60_000 },
  );
  return res.data;
}

export interface HelpFaqItem {
  q: string;
  a: string;
}

export interface HelpSection {
  name: string;
  slug: string;
  description: string;
  faqs: HelpFaqItem[];
}

/** FAQs del manual agrupadas por sección (centro de ayuda — no usa LLM). */
export async function getHelpFaqs(): Promise<HelpSection[]> {
  const res = await api.get<HelpSection[]>("/agent/help/faqs");
  return res.data;
}

export async function confirmAction(pendingActionId: string): Promise<ConfirmActionResponse> {
  const res = await api.post<ConfirmActionResponse>(`/agent/confirm/${pendingActionId}`);
  return res.data;
}

export async function cancelAction(pendingActionId: string): Promise<void> {
  await api.post(`/agent/cancel/${pendingActionId}`);
}

export interface ConfirmGroupResponse {
  status: string;
  approval_group_id: string;
  group_execution_status: "SUCCEEDED" | "PARTIAL_FAILED";
  tasks: Array<{
    action_id: string;
    action_type: string;
    execution_status: string;
  }>;
}

export async function confirmGroup(groupId: string): Promise<ConfirmGroupResponse> {
  const res = await api.post<ConfirmGroupResponse>(`/agent/confirm/group/${groupId}`);
  return res.data;
}

export async function cancelGroup(groupId: string): Promise<void> {
  await api.post(`/agent/cancel/group/${groupId}`);
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
