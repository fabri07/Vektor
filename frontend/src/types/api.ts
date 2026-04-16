export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

export interface PydanticError {
  type: string;
  loc: (string | number)[];
  msg: string;
  input: unknown;
  ctx?: Record<string, unknown>;
}

export interface ApiError {
  detail: string | PydanticError[];
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
}

export interface AuthUserResponse {
  user_id: string;
  email: string;
  full_name: string;
  role_code: string;
  tenant_id: string;
}

export interface MeResponse {
  user_id: string;
  email: string;
  full_name: string;
  role_code: string;
  tenant_id: string;
  subscription: {
    plan_code: string;
    status: string;
  } | null;
  onboarding_completed: boolean;
}

export interface AuthResponse {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
  user: AuthUserResponse;
}

export interface RegisterResponse {
  message: string;
  email: string;
  requires_verification: boolean;
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

// ── Notifications ─────────────────────────────────────────────────────────────

export interface NotificationItem {
  id: string;
  title: string;
  body: string;
  notification_type: string;
  channel: string;
  is_read: boolean;
  created_at: string;
}

export interface NotificationListResponse {
  notifications: NotificationItem[];
  unread_count: number;
}

// ── Momentum ──────────────────────────────────────────────────────────────────

export interface WeeklyHistoryItem {
  week_start: string;
  week_end: string;
  avg_score: number;
  delta: number | null;
  trend_label: string | null;
}

export interface ActiveGoalResponse {
  weak_dimension: string;
  goal: string;
  action: string;
  estimated_delta: number;
  estimated_weeks: number;
}

export interface MilestoneItem {
  code: string;
  label: string;
  unlocked_at: string;
}

export interface MomentumProfileResponse {
  best_score_ever: number | null;
  best_score_date: string | null;
  active_goal: ActiveGoalResponse | null;
  milestones_unlocked: MilestoneItem[];
  estimated_value_protected_ars: number;
  improving_streak_weeks: number;
  weekly_history: WeeklyHistoryItem[];
}

// ── Sales ─────────────────────────────────────────────────────────────────────

export interface SaleSummaryResponse {
  total_ars: number;
  entry_count: number;
  period_covered: string;
}

// ── Expenses ──────────────────────────────────────────────────────────────────

export interface ExpenseSummaryResponse {
  total_ars: number;
  entry_count: number;
  period_covered: string;
}

// ── Files ─────────────────────────────────────────────────────────────────────

export interface UploadedFileResponse {
  id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  purpose: string;
  status: string;
}

// ── OAuth / Google Login ──────────────────────────────────────────────────────

export interface OAuthStartResponse {
  authorization_url: string;
}

export interface OAuthLinkRequiredResponse {
  status: "link_required";
  pending_oauth_session_id: string;
  email: string;
  provider: "google";
}

// ── Google Workspace ─────────────────────────────────────────────────────────

export interface WorkspaceConnectStartResponse {
  authorization_url: string;
}

export interface WorkspaceAppStatus {
  id: string;
  label: string;
  description: string;
  available: boolean;
  connected: boolean;
  needs_reconnect: boolean;
  required_scopes: string[];
}

export interface WorkspaceStatusResponse {
  connected: boolean;
  google_account_email: string | null;
  scopes_granted: string[];
  apps: WorkspaceAppStatus[];
  connected_at: string | null;
  last_error_code: string | null;
}
