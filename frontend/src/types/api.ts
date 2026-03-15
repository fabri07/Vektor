export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

export interface ApiError {
  detail: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
}

export interface UserResponse {
  id: string;
  email: string;
  full_name: string;
  role: string;
  tenant_id: string;
  is_active: boolean;
  created_at: string;
}

export interface HealthScoreV2Response {
  id: string;
  tenant_id: string;
  score_total: number;
  score_cash: number;
  score_margin: number;
  score_stock: number;
  score_supplier: number;
  primary_risk_code: string;
  confidence_level: string;
  data_completeness_score: number;
  level: string;
  created_at: string;
}

export interface CalculatingResponse {
  status: "CALCULATING";
}

export type LatestScoreResponse = HealthScoreV2Response | CalculatingResponse;

export interface InsightResponse {
  id: string;
  title: string;
  description: string;
  insight_type: string;
  severity_code: string;
  heuristic_version: string;
  created_at: string;
}

export interface ActionSuggestionResponse {
  id: string;
  title: string;
  description: string;
  action_type: string;
  risk_level: string;
  status: string;
  created_at: string;
}

export interface CurrentInsightResponse {
  insight: InsightResponse;
  action_suggestion: ActionSuggestionResponse | null;
}
