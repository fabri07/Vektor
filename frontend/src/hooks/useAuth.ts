import { useMutation } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { loginRequest, getMeRequest } from "@/services/auth.service";
import type { LoginInput } from "@/validation/auth";

export function useLogin() {
  const setAuth = useAuthStore((s) => s.setAuth);

  return useMutation({
    mutationFn: async (data: LoginInput) => {
      const { access_token } = await loginRequest(data);
      const user = await getMeRequest({
        headers: { Authorization: `Bearer ${access_token}` },
      });
      return { access_token, user };
    },
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
