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

/** POST /users — alta de un usuario del equipo (sólo el dueño, con PIN). */
export async function createTeamUserRequest(data: {
  email: string;
  full_name: string;
  role_code: string;
  password: string;
}): Promise<UserResponse> {
  const res = await api.post<UserResponse>("/users", data);
  return res.data;
}
