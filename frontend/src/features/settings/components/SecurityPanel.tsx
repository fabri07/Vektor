"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";
import { usePinGateStore } from "@/stores/pinGateStore";
import { securityService, type TeamMember } from "@/services/security.service";
import { createTeamUserRequest } from "@/services/users.service";
import { CASHIER_ROLE, POS_PERMISSIONS, roleLabel } from "@/lib/roles";

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

function errorDetalle(e: unknown, porDefecto: string): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const d = detail as { code?: string; message?: string };
    if (d.code === "SEAT_LIMIT_EXCEEDED") return "Tu plan no tiene más usuarios disponibles.";
    if (d.message) return d.message;
  }
  return porDefecto;
}

function AddCashierForm({ onDone }: { onDone: () => void }) {
  const toast = useToastStore((s) => s.add);
  const queryClient = useQueryClient();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const crear = useMutation({
    mutationFn: () =>
      createTeamUserRequest({
        full_name: fullName.trim(),
        email: email.trim(),
        password,
        role_code: CASHIER_ROLE,
      }),
    onSuccess: async () => {
      toast("Cajero creado. Ahora elegí qué puede hacer.", "success");
      setFullName("");
      setEmail("");
      setPassword("");
      await queryClient.invalidateQueries({ queryKey: ["team-permissions"] });
      onDone();
    },
    onError: (e) => toast(errorDetalle(e, "No se pudo crear el cajero."), "error"),
  });

  const valido = fullName.trim().length >= 2 && email.includes("@") && password.length >= 8;

  return (
    <form
      className="mt-4 grid gap-2 rounded-lg border border-vk-border-w p-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (valido) crear.mutate();
      }}
    >
      <p className="text-xs font-medium text-vk-text-primary">Agregar cajero</p>
      <input
        className="rounded border border-vk-border-w px-3 py-2 text-sm"
        placeholder="Nombre y apellido"
        value={fullName}
        onChange={(e) => setFullName(e.target.value)}
      />
      <input
        className="rounded border border-vk-border-w px-3 py-2 text-sm"
        placeholder="Email"
        type="email"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
      />
      <input
        className="rounded border border-vk-border-w px-3 py-2 text-sm"
        placeholder="Contraseña inicial (mínimo 8 caracteres)"
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
      />
      <p className="text-xs text-vk-text-muted">
        Ocupa un usuario de tu plan. Nace sin permisos de caja: los elegís en la lista.
      </p>
      <Button type="submit" size="sm" disabled={!valido || crear.isPending}>
        {crear.isPending ? "Creando…" : "Crear cajero"}
      </Button>
    </form>
  );
}

function CashierPermissions({ member }: { member: TeamMember }) {
  const toast = useToastStore((s) => s.add);
  const queryClient = useQueryClient();
  const actuales = new Set(member.pos_permissions ?? []);

  const guardar = useMutation({
    mutationFn: (permisos: Record<string, boolean>) =>
      securityService.setTeamPosPermissions(member.user_id, permisos),
    onSuccess: async () => {
      toast("Permisos de caja actualizados.", "success");
      await queryClient.invalidateQueries({ queryKey: ["team-permissions"] });
    },
    onError: () => toast("No se pudieron actualizar los permisos.", "error"),
  });

  return (
    <div className="mt-2 grid gap-1">
      {POS_PERMISSIONS.map((p) => (
        <label key={p.key} className="flex items-center gap-2 text-xs text-vk-text-secondary">
          <input
            type="checkbox"
            checked={actuales.has(p.key)}
            disabled={guardar.isPending}
            onChange={(e) => {
              // Se manda el set COMPLETO: lo que no está marcado queda en false.
              const permisos: Record<string, boolean> = {};
              for (const q of POS_PERMISSIONS) {
                permisos[q.key] = q.key === p.key ? e.target.checked : actuales.has(q.key);
              }
              guardar.mutate(permisos);
            }}
            className="h-4 w-4 accent-vk-blue"
          />
          {p.label}
        </label>
      ))}
    </div>
  );
}

function TeamSection() {
  const toast = useToastStore((s) => s.add);
  const queryClient = useQueryClient();
  const [show, setShow] = useState(false);
  const [agregando, setAgregando] = useState(false);

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

  const miembros = team.filter((m: TeamMember) => m.role_code !== "OWNER");

  return (
    <section className="rounded-xl border border-vk-border-w bg-vk-surface-w p-5">
      <h3 className="text-sm font-semibold text-vk-text-primary">Equipo y permisos</h3>
      <p className="mt-1 text-xs text-vk-text-secondary">
        Autorizá a sub-cuentas a modificar datos y elegí qué puede hacer cada cajero. Cada uno
        usa su propio PIN.
      </p>

      {!show ? (
        <Button size="sm" variant="secondary" className="mt-4" onClick={() => setShow(true)}>
          Administrar equipo
        </Button>
      ) : isLoading ? (
        <p className="mt-4 text-sm text-vk-text-muted">Cargando…</p>
      ) : (
        <>
          {miembros.length === 0 ? (
            <p className="mt-4 text-sm text-vk-text-muted">No tenés sub-cuentas todavía.</p>
          ) : (
            <ul className="mt-4 divide-y divide-vk-border-w">
              {miembros.map((m: TeamMember) => (
                <li key={m.user_id} className="py-3">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <p className="text-sm text-vk-text-primary">{m.full_name}</p>
                      <p className="text-xs text-vk-text-muted">
                        {m.email} · {roleLabel(m.role_code)}
                        {m.pin_set ? " · PIN configurado" : " · sin PIN"}
                      </p>
                    </div>
                    {m.role_code !== CASHIER_ROLE && (
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
                    )}
                  </div>
                  {m.role_code === CASHIER_ROLE && <CashierPermissions member={m} />}
                </li>
              ))}
            </ul>
          )}
          {agregando ? (
            <AddCashierForm onDone={() => setAgregando(false)} />
          ) : (
            <Button
              size="sm"
              variant="secondary"
              className="mt-4"
              onClick={() => setAgregando(true)}
            >
              Agregar cajero
            </Button>
          )}
        </>
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
