import { useMutation } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { loginRequest, registerRequest, verifyEmailRequest } from "@/services/auth.service";
import type { LoginInput, RegisterInput } from "@/validation/auth";

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
      });
    },
  });
}

export function useRegister() {
  // Registration no longer issues tokens — user must verify email first.
  // The caller (RegisterForm) handles the redirect to /verify-email-sent.
  return useMutation({
    mutationFn: (data: RegisterInput) => registerRequest(data),
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
      });
    },
  });
}
