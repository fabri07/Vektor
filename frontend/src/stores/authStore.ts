import { create } from "zustand";
import { persist } from "zustand/middleware";

interface AuthUser {
  id: string;
  email: string;
  full_name: string;
  role: string;
  tenant_id: string;
}

interface AuthState {
  token: string | null;
  user: AuthUser | null;
  _hasHydrated: boolean;
  setAuth: (token: string, user: AuthUser) => void;
  setHasHydrated: (state: boolean) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      user: null,
      _hasHydrated: false,
      setAuth: (token, user) => set({ token, user }),
      setHasHydrated: (state) => set({ _hasHydrated: state }),
      logout: () => {
        set({ token: null, user: null });
        if (typeof window !== "undefined") {
          window.location.href = "/login";
        }
      },
    }),
    {
      name: "vektor_auth",
      partialize: (state) => ({ token: state.token, user: state.user }),
      onRehydrateStorage: () => (state) => {
        state?.setHasHydrated(true);
      },
    },
  ),
);
