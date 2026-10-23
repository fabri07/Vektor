import { useMutation } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { loginRequest, verifyEmailRequest } from "@/services/auth.service";
import type { LoginInput } from "@/validation/auth";

export function useLogin() {
  const setAuth = useAuthStore((s) => s.setAuth);

  return useMutation({
    mutationFn: (data: LoginInput) => loginRequest(data),
    onSuccess: ({ access_token, refresh_token, user }) => {
      setAuth(access_token, refresh_token, {
        id: user.user_id,
        email: user.email,
        full_name: user.full_name,
        role: user.role_code,
        tenant_id: user.tenant_id,
        phone: user.phone ?? null,
      });
    },
  });
}

export function useVerifyEmail() {
  const setAuth = useAuthStore((s) => s.setAuth);

  return useMutation({
    mutationFn: (token: string) => verifyEmailRequest(token),
    onSuccess: ({ access_token, refresh_token, user }) => {
      setAuth(access_token, refresh_token, {
        id: user.user_id,
        email: user.email,
        full_name: user.full_name,
        role: user.role_code,
        tenant_id: user.tenant_id,
        phone: user.phone ?? null,
      });
    },
  });
}
