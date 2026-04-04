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
}

export async function sendMessage(
  message: string,
  conversationId?: string
): Promise<AgentResponse> {
  const res = await api.post<AgentResponse>("/agent/chat", {
    message,
    conversation_id: conversationId,
  });
  return res.data;
}

export async function confirmAction(pendingActionId: string): Promise<void> {
  await api.post(`/agent/confirm/${pendingActionId}`);
}

export async function cancelAction(pendingActionId: string): Promise<void> {
  await api.post(`/agent/cancel/${pendingActionId}`);
}
