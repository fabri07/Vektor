import { api } from "@/lib/api";
import type { TokenResponse, UserResponse } from "@/types/api";
import type { LoginInput } from "@/validation/auth";

export async function loginRequest(data: LoginInput): Promise<TokenResponse> {
  const form = new URLSearchParams();
  form.append("username", data.email);
  form.append("password", data.password);
  const res = await api.post<TokenResponse>("/auth/login", form, {
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  });
  return res.data;
}

export async function getMeRequest(): Promise<UserResponse> {
  const res = await api.get<UserResponse>("/auth/me");
  return res.data;
}

export async function logoutRequest(): Promise<void> {
  await api.post("/auth/logout");
}
