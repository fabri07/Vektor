import { useMutation } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { loginRequest, registerRequest } from "@/services/auth.service";
import type { LoginInput, RegisterInput } from "@/validation/auth";

export function useLogin() {
  const setAuth = useAuthStore((s) => s.setAuth);

  return useMutation({
    mutationFn: (data: LoginInput) => loginRequest(data),
    onSuccess: ({ access_token, user }) => {
      setAuth(access_token, {
        id: user.id,
        email: user.email,
        full_name: user.full_name,
        role: user.role,
        tenant_id: user.tenant_id,
      });
    },
  });
}

export function useRegister() {
  const setAuth = useAuthStore((s) => s.setAuth);

  return useMutation({
    mutationFn: (data: RegisterInput) => registerRequest(data),
    onSuccess: ({ access_token, user }) => {
      setAuth(access_token, {
        id: user.id,
        email: user.email,
        full_name: user.full_name,
        role: user.role,
        tenant_id: user.tenant_id,
      });
    },
  });
}
