import { api } from "@/lib/api";

export interface AutomationRule {
  id: string;
  agent_name: string;
  action_type: string;
  external_system: string | null;
  rule_key: string;
  criteria: Record<string, unknown>;
  enabled: boolean;
  risk_level: string;
  created_from_pending_action_id: string | null;
  last_executed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AutomationOffer {
  pending_action_id: string;
  agent_name: string;
  action_type: string;
  external_system: string | null;
  rule_key: string;
  criteria: Record<string, unknown>;
  risk_level: string;
  message: string;
}

export const automationsService = {
  async list(): Promise<AutomationRule[]> {
    const res = await api.get<AutomationRule[]>("/automations");
    return res.data;
  },

  async createFromPendingAction(pendingActionId: string): Promise<AutomationRule> {
    const res = await api.post<{ rule: AutomationRule; created: boolean }>(
      `/automations/from-pending-action/${pendingActionId}`,
    );
    return res.data.rule;
  },

  async setEnabled(ruleId: string, enabled: boolean): Promise<AutomationRule> {
    const res = await api.patch<AutomationRule>(`/automations/${ruleId}`, { enabled });
    return res.data;
  },

  async remove(ruleId: string): Promise<void> {
    await api.delete(`/automations/${ruleId}`);
  },
};
