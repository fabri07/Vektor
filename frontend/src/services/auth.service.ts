import { api } from "@/lib/api";
import type {
  AuthResponse,
  MeResponse,
  RegisterResponse,
  OAuthStartResponse,
  OAuthLinkRequiredResponse,
} from "@/types/api";
import type { LoginInput, RegisterInput } from "@/validation/auth";

export async function loginRequest(data: LoginInput): Promise<AuthResponse> {
  const res = await api.post<AuthResponse>("/auth/login", {
    email: data.email,
    password: data.password,
  });
  return res.data;
}

export async function registerRequest(data: RegisterInput): Promise<RegisterResponse> {
  const res = await api.post<RegisterResponse>("/auth/register", {
    email: data.email,
    password: data.password,
    full_name: data.full_name,
    business_name: data.business_name,
    vertical_code: data.vertical_code,
  });
  return res.data;
}

export async function verifyEmailRequest(token: string): Promise<AuthResponse> {
  const res = await api.post<AuthResponse>("/auth/verify-email", { token });
  return res.data;
}

export async function resendVerificationRequest(email: string): Promise<void> {
  await api.post("/auth/resend-verification", { email });
}

export async function getMeRequest(config?: {
  headers?: Record<string, string>;
}): Promise<MeResponse> {
  const res = await api.get<MeResponse>("/auth/me", config);
  return res.data;
}

export async function logoutRequest(): Promise<void> {
  await api.post("/auth/logout");
}

export async function getGoogleOAuthUrl(): Promise<OAuthStartResponse> {
  const res = await api.post<OAuthStartResponse>("/auth/oauth/google/start");
  return res.data;
}

export async function exchangeGoogleSession(
  session_id: string,
): Promise<AuthResponse | OAuthLinkRequiredResponse> {
  const res = await api.post<AuthResponse | OAuthLinkRequiredResponse>(
    "/auth/oauth/google/exchange",
    { session_id },
  );
  return res.data;
}

export async function linkPendingOAuth(data: {
  pending_oauth_session_id: string;
  email: string;
  password: string;
}): Promise<AuthResponse> {
  const res = await api.post<AuthResponse>("/auth/oauth/google/link-pending", data);
  return res.data;
}
