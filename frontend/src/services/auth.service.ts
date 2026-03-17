import { api } from "@/lib/api";
import type { AuthResponse, UserResponse } from "@/types/api";
import type { LoginInput, RegisterInput } from "@/validation/auth";

export async function loginRequest(data: LoginInput): Promise<AuthResponse> {
  const res = await api.post<AuthResponse>("/auth/login", {
    email: data.email,
    password: data.password,
  });
  return res.data;
}

export async function registerRequest(data: RegisterInput): Promise<AuthResponse> {
  const res = await api.post<AuthResponse>("/auth/register", {
    email: data.email,
    password: data.password,
    full_name: data.full_name,
    business_name: data.business_name,
    vertical_code: data.vertical_code,
  });
  return res.data;
}

export async function getMeRequest(config?: {
  headers?: Record<string, string>;
}): Promise<UserResponse> {
  const res = await api.get<UserResponse>("/auth/me", config);
  return res.data;
}

export async function logoutRequest(): Promise<void> {
  await api.post("/auth/logout");
}
