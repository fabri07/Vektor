"use client";

import { useAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/Button";

export function Header() {
  const { user, logout } = useAuthStore();

  return (
    <header className="flex h-14 items-center justify-between border-b border-white/10 bg-primary/80 px-6 backdrop-blur">
      <div />
      <div className="flex items-center gap-4">
        {user && (
          <span className="text-sm text-white/50">{user.email}</span>
        )}
        <Button variant="ghost" size="sm" onClick={logout}>
          Salir
        </Button>
      </div>
    </header>
  );
}
