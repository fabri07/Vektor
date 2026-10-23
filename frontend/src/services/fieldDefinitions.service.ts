import { api } from "@/lib/api";
import type {
  CreateCustomFieldPayload,
  FieldChangeLog,
  FieldDefinition,
} from "@/types/api";

const BASE = "/fields/definitions";

export const fieldDefinitionsService = {
  async getAll(entityType?: string): Promise<FieldDefinition[]> {
    const params = entityType ? { entity_type: entityType } : {};
    const res = await api.get<FieldDefinition[]>(BASE, { params });
    return res.data;
  },

  async create(payload: CreateCustomFieldPayload): Promise<FieldDefinition> {
    const res = await api.post<FieldDefinition>(BASE, payload);
    return res.data;
  },

  async toggle(
    fieldKey: string,
    entityType: string,
    enabled: boolean
  ): Promise<FieldDefinition> {
    const res = await api.post<FieldDefinition>(
      `${BASE}/${fieldKey}/toggle`,
      { enabled },
      { params: { entity_type: entityType } }
    );
    return res.data;
  },

  async undo(fieldKey: string, entityType: string): Promise<void> {
    await api.post(`${BASE}/${fieldKey}/undo`, null, {
      params: { entity_type: entityType },
    });
  },

  async getHistory(limit = 50): Promise<FieldChangeLog[]> {
    const res = await api.get<FieldChangeLog[]>(`${BASE}/history`, {
      params: { limit },
    });
    return res.data;
  },
};
