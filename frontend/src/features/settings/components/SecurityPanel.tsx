"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";
import { usePinGateStore } from "@/stores/pinGateStore";
import { securityService, type TeamMember } from "@/services/security.service";

const PIN_RE = /^\d{4}$/;

function PinInput({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs text-vk-text-muted">{label}</span>
      <input
        type="password"
        inputMode="numeric"
        autoComplete="off"
        maxLength={4}
        value={value}
        onChange={(e) => onChange(e.target.value.replace(/\D/g, "").slice(0, 4))}
        className="w-32 rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-2 text-center text-lg tracking-[0.4em] text-vk-text-primary focus:border-vk-blue focus:outline-none"
        placeholder="••••"
      />
    </label>
  );
}

function MyPinSection() {
  const toast = useToastStore((s) => s.add);
  const openSetup = usePinGateStore((s) => s.openSetup);
  const { data: status, refetch } = useQuery({
    queryKey: ["pin-status"],
    queryFn: () => securityService.pinStatus(),
  });

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");

  const changeMutation = useMutation({
    mutationFn: () => securityService.changePin(current, next, confirm),
    onSuccess: async () => {
      toast("PIN actualizado.", "success");
      setCurrent("");
      setNext("");
      setConfirm("");
      await refetch();
    },
    onError: (err) => {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data
        ?.detail;
      toast(typeof detail === "string" ? detail : "No se pudo cambiar el PIN.", "error");
    },
  });

  function submit() {
    if (![current, next, confirm].every((p) => PIN_RE.test(p))) {
      toast("Los PIN deben tener 4 dígitos.", "error");
      return;
    }
    if (next !== confirm) {
      toast("El nuevo PIN no coincide.", "error");
      return;
    }
    changeMutation.mutate();
  }

  return (
    <section className="rounded-xl border border-vk-border-w bg-vk-surface-w p-5">
      <h3 className="text-sm font-semibold text-vk-text-primary">Mi PIN de seguridad</h3>
      <p className="mt-1 text-xs text-vk-text-secondary">
        Te lo pedimos antes de modificar datos sensibles (editar/borrar registros,
        configuraciones, releer archivos, retiro de ganancias).
      </p>

      {!status?.pin_set ? (
        <div className="mt-4">
          <p className="mb-2 text-sm text-vk-text-secondary">Todavía no configuraste un PIN.</p>
          <Button size="sm" onClick={() => openSetup()}>
            Configurar PIN
          </Button>
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap items-end gap-4">
          <PinInput label="PIN actual" value={current} onChange={setCurrent} />
          <PinInput label="Nuevo PIN" value={next} onChange={setNext} />
          <PinInput label="Repetir nuevo PIN" value={confirm} onChange={setConfirm} />
          <Button size="sm" onClick={submit} loading={changeMutation.isPending}>
            Cambiar PIN
          </Button>
        </div>
      )}
    </section>
  );
}

function TeamSection() {
  const toast = useToastStore((s) => s.add);
  const queryClient = useQueryClient();
  const [show, setShow] = useState(false);

  const { data: team = [], isLoading } = useQuery({
    queryKey: ["team-permissions"],
    queryFn: () => securityService.listTeam(),
    enabled: show, // No dispara PIN hasta que el OWNER lo pide explícitamente.
  });

  const toggleMutation = useMutation({
    mutationFn: ({ userId, canModify }: { userId: string; canModify: boolean }) =>
      securityService.setTeamPermission(userId, canModify),
    onSuccess: async () => {
      toast("Permiso actualizado.", "success");
      await queryClient.invalidateQueries({ queryKey: ["team-permissions"] });
    },
    onError: () => toast("No se pudo actualizar el permiso.", "error"),
  });

  return (
    <section className="rounded-xl border border-vk-border-w bg-vk-surface-w p-5">
      <h3 className="text-sm font-semibold text-vk-text-primary">Permisos del equipo</h3>
      <p className="mt-1 text-xs text-vk-text-secondary">
        Autorizá a sub-cuentas a modificar datos. Cada una usa su propio PIN.
      </p>

      {!show ? (
        <Button size="sm" variant="secondary" className="mt-4" onClick={() => setShow(true)}>
          Administrar permisos
        </Button>
      ) : isLoading ? (
        <p className="mt-4 text-sm text-vk-text-muted">Cargando…</p>
      ) : team.length <= 1 ? (
        <p className="mt-4 text-sm text-vk-text-muted">
          No tenés sub-cuentas todavía.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-vk-border-w">
          {team
            .filter((m: TeamMember) => m.role_code !== "OWNER")
            .map((m: TeamMember) => (
              <li key={m.user_id} className="flex items-center justify-between py-3">
                <div>
                  <p className="text-sm text-vk-text-primary">{m.full_name}</p>
                  <p className="text-xs text-vk-text-muted">
                    {m.email} · {m.role_code}
                    {m.pin_set ? " · PIN configurado" : " · sin PIN"}
                  </p>
                </div>
                <label className="flex items-center gap-2 text-xs text-vk-text-secondary">
                  Puede modificar datos
                  <input
                    type="checkbox"
                    checked={m.can_modify_sensitive}
                    disabled={toggleMutation.isPending}
                    onChange={(e) =>
                      toggleMutation.mutate({
                        userId: m.user_id,
                        canModify: e.target.checked,
                      })
                    }
                    className="h-4 w-4 accent-vk-blue"
                  />
                </label>
              </li>
            ))}
        </ul>
      )}
    </section>
  );
}

export function SecurityPanel() {
  const role = useAuthStore((s) => s.user?.role);
  return (
    <div className="space-y-5">
      <MyPinSection />
      {role === "OWNER" && <TeamSection />}
    </div>
  );
}
