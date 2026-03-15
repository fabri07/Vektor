import { api } from "@/lib/api";
import type { MomentumProfileResponse } from "@/types/api";

export async function fetchMomentumProfile(): Promise<MomentumProfileResponse> {
  const { data } = await api.get<MomentumProfileResponse>("/momentum/profile");
  return data;
}
