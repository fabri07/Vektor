import { api } from "@/lib/api";

export interface PinStatus {
  pin_set: boolean;
  verified: boolean;
  must_set: boolean;
}

export interface TeamMember {
  user_id: string;
  email: string;
  full_name: string;
  role_code: string;
  can_modify_sensitive: boolean;
  pin_set: boolean;
}

export const securityService = {
  async pinStatus(): Promise<PinStatus> {
    const res = await api.get<PinStatus>("/auth/pin/status");
    return res.data;
  },
  async setupPin(pin: string, pinConfirm: string): Promise<void> {
    await api.post("/auth/pin/setup", { pin, pin_confirm: pinConfirm });
  },
  async verifyPin(pin: string): Promise<void> {
    await api.post("/auth/pin/verify", { pin });
  },
  async changePin(currentPin: string, newPin: string, newPinConfirm: string): Promise<void> {
    await api.post("/auth/pin/change", {
      current_pin: currentPin,
      new_pin: newPin,
      new_pin_confirm: newPinConfirm,
    });
  },
  async resetPin(password: string, newPin: string, newPinConfirm: string): Promise<void> {
    await api.post("/auth/pin/reset", {
      password,
      new_pin: newPin,
      new_pin_confirm: newPinConfirm,
    });
  },
  async listTeam(): Promise<TeamMember[]> {
    const res = await api.get<TeamMember[]>("/settings/team");
    return res.data;
  },
  async setTeamPermission(userId: string, canModify: boolean): Promise<TeamMember> {
    const res = await api.patch<TeamMember>(`/settings/team/${userId}`, {
      can_modify_sensitive: canModify,
    });
    return res.data;
  },
};
