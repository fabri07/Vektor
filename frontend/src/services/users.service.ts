import { api } from "@/lib/api";
import type { UserResponse } from "@/types/api";

/** PATCH /users/me — perfil propio (nombre + teléfono). ``phone: null`` borra. */
export async function updateMeRequest(data: {
  full_name?: string;
  phone?: string | null;
}): Promise<UserResponse> {
  const res = await api.patch<UserResponse>("/users/me", data);
  return res.data;
}
