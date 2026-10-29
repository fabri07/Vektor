"use client";

import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { EmptyState } from "@/components/ui/EmptyState";
import { useToastStore } from "@/stores/toastStore";
import { adminErrorMessage, adminTenantsService } from "@/services/adminTenants.service";
import type { EstadoDeCuenta } from "@/services/adminTenants.service";
import {
  CONSECUENCIAS,
  ESTADOS_DE_CUENTA,
  entero,
  fechaCorta,
  fechaYHora,
  rotuloDeCuenta,
  rotuloDeSuscripcion,
} from "./format";

const PLANES = ["esencial", "control", "direccion"];

const inputClass =
  "h-9 w-full rounded-md border border-vk-border-w bg-vk-surface-w px-3 text-sm text-vk-text-primary";
const buttonClass =
  "rounded-md bg-vk-text-primary px-3 py-2 text-sm font-medium text-vk-surface-w disabled:opacity-40";
const dangerClass =
  "rounded-md bg-vk-danger px-3 py-2 text-sm font-medium text-vk-surface-w disabled:opacity-40";

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
      <h2 className="mb-4 text-sm font-semibold text-vk-text-primary">{title}</h2>
      {children}
    </section>
  );
}

function Dato({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-vk-text-muted">{label}</dt>
      <dd className="text-sm text-vk-text-primary">{value}</dd>
    </div>
  );
}

function ErrorText({ mensaje }: { mensaje: string | null }) {
  if (!mensaje) return null;
  return (
    <p role="alert" className="text-sm text-vk-danger">
      {mensaje}
    </p>
  );
}

/** Ficha de un negocio: estado de cuenta, suscripción, pagos, usuarios y auditoría. */
export function TenantDetailScreen({ tenantId }: { tenantId: string }) {
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const detalle = useQuery({
    queryKey: ["admin-tenant", tenantId],
    queryFn: () => adminTenantsService.get(tenantId),
  });
  const pagos = useQuery({
    queryKey: ["admin-tenant-payments", tenantId],
    queryFn: () => adminTenantsService.listPayments(tenantId),
  });

  const refrescar = () => {
    void queryClient.invalidateQueries({ queryKey: ["admin-tenant", tenantId] });
    void queryClient.invalidateQueries({ queryKey: ["admin-tenant-payments", tenantId] });
    void queryClient.invalidateQueries({ queryKey: ["admin-tenants"] });
  };

  // ── Estado de cuenta ──
  const [nuevoEstado, setNuevoEstado] = useState<EstadoDeCuenta | "">("");
  const [motivo, setMotivo] = useState("");
  const estado = useMutation({
    mutationFn: () =>
      adminTenantsService.changeStatus(tenantId, {
        status: nuevoEstado as EstadoDeCuenta,
        reason: motivo.trim(),
      }),
    onSuccess: (r) => {
      toast(r.changed ? "Estado de cuenta actualizado." : "La cuenta ya estaba en ese estado.", "success");
      setNuevoEstado("");
      setMotivo("");
      refrescar();
    },
  });

  // ── Pago ──
  const [pago, setPago] = useState({
    expected: "renew" as "activate" | "renew",
    reference: "",
    amount_usd: "",
    amount_ars: "",
    plan_code: "",
    notes: "",
  });
  const registrarPago = useMutation({
    mutationFn: () =>
      adminTenantsService.applyPayment(tenantId, {
        expected: pago.expected,
        reference: pago.reference.trim(),
        amount_usd: pago.amount_usd,
        amount_ars: pago.amount_ars,
        plan_code: pago.plan_code.trim() || null,
        notes: pago.notes.trim() || null,
      }),
    onSuccess: (r) => {
      toast(
        r.already_applied
          ? "Esa referencia ya estaba acreditada: no se otorgó nada nuevo."
          : `Pago acreditado. Acceso hasta ${fechaCorta(r.period_end)}.`,
        "success",
      );
      setPago({ ...pago, reference: "", amount_usd: "", amount_ars: "", notes: "" });
      refrescar();
    },
  });

  // ── Plan / cancelación ──
  const [plan, setPlan] = useState("");
  const cambiarPlan = useMutation({
    mutationFn: () => adminTenantsService.changePlan(tenantId, plan.trim()),
    onSuccess: (r) => {
      toast(`Plan programado para la próxima renovación: ${r.scheduled_plan_code}.`, "success");
      setPlan("");
      refrescar();
    },
  });
  const [confirmaCancelar, setConfirmaCancelar] = useState(false);
  const [motivoCancelar, setMotivoCancelar] = useState("");
  const cancelar = useMutation({
    mutationFn: () => adminTenantsService.cancelRenewal(tenantId, motivoCancelar.trim() || null),
    onSuccess: (r) => {
      toast(`Renovación cancelada. Conserva el acceso hasta ${fechaCorta(r.access_until)}.`, "success");
      setConfirmaCancelar(false);
      setMotivoCancelar("");
      refrescar();
    },
  });

  if (detalle.isLoading) {
    return (
      <PageWrapper title="Negocio">
        <div className="p-10 text-center text-sm text-vk-text-muted">Cargando…</div>
      </PageWrapper>
    );
  }
  if (detalle.isError || !detalle.data) {
    return (
      <PageWrapper title="Negocio">
        <EmptyState
          title="No se pudo cargar el negocio"
          description="No existe o ocurrió un error al consultarlo."
        />
      </PageWrapper>
    );
  }

  const t = detalle.data;
  const sub = t.subscription;
  const estadosPosibles = ESTADOS_DE_CUENTA.filter((e) => e.value !== t.account_status);

  return (
    <PageWrapper
      title={t.display_name}
      actions={
        <Link href="/admin/tenants" className="text-sm text-vk-text-muted hover:underline">
          ← Volver a negocios
        </Link>
      }
    >
      <Panel title="Cuenta">
        <dl className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-4">
          <Dato label="Estado de cuenta" value={rotuloDeCuenta(t.account_status)} />
          <Dato label="Desde" value={fechaYHora(t.status_changed_at)} />
          <Dato label="Rubro" value={t.vertical_code ?? "—"} />
          <Dato label="Alta" value={fechaCorta(t.created_at)} />
        </dl>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (nuevoEstado && motivo.trim().length >= 3) estado.mutate();
          }}
        >
          <label className="text-xs text-vk-text-muted" htmlFor="nuevo-estado">
            Cambiar estado de cuenta
          </label>
          <select
            id="nuevo-estado"
            value={nuevoEstado}
            onChange={(e) => setNuevoEstado(e.target.value as EstadoDeCuenta | "")}
            className={inputClass}
          >
            <option value="">Elegí el nuevo estado…</option>
            {estadosPosibles.map((e) => (
              <option key={e.value} value={e.value}>
                {e.label}
              </option>
            ))}
          </select>
          {nuevoEstado && (
            <p className="rounded-md bg-vk-bg-light p-3 text-sm text-vk-text-secondary">
              {CONSECUENCIAS[nuevoEstado]}
            </p>
          )}
          <textarea
            value={motivo}
            onChange={(e) => setMotivo(e.target.value)}
            placeholder="Motivo (obligatorio, queda auditado)"
            aria-label="Motivo del cambio de estado"
            maxLength={300}
            rows={2}
            className="w-full rounded-md border border-vk-border-w bg-vk-surface-w p-3 text-sm"
          />
          <ErrorText mensaje={estado.isError ? adminErrorMessage(estado.error) : null} />
          <div>
            <button
              type="submit"
              disabled={!nuevoEstado || motivo.trim().length < 3 || estado.isPending}
              className={nuevoEstado === "SUSPENDED" ? dangerClass : buttonClass}
            >
              {nuevoEstado === "SUSPENDED" ? "Suspender cuenta" : "Cambiar estado"}
            </button>
          </div>
        </form>
      </Panel>

      <Panel title="Suscripción">
        {sub ? (
          <dl className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-4">
            <Dato label="Plan" value={sub.plan_code} />
            <Dato label="Estado" value={rotuloDeSuscripcion(sub.status)} />
            <Dato label="Pago hasta" value={fechaCorta(sub.current_period_end)} />
            <Dato label="Prueba hasta" value={fechaCorta(sub.trial_ends_at)} />
            <Dato label="Usuarios incluidos" value={entero(sub.seats_included)} />
            <Dato label="Consultas IA / mes" value={sub.granted_ia_queries_per_month ?? "—"} />
            <Dato label="Importaciones / mes" value={sub.granted_imports_per_month ?? "—"} />
            <Dato
              label="Renovación"
              value={sub.cancel_at_period_end ? "Cancelada" : "Vigente"}
            />
          </dl>
        ) : (
          <p className="mb-4 text-sm text-vk-text-muted">El negocio no tiene suscripción.</p>
        )}

        {sub && (
          <div className="flex flex-col gap-6">
            <form
              className="grid grid-cols-1 gap-3 md:grid-cols-3"
              onSubmit={(e) => {
                e.preventDefault();
                registrarPago.mutate();
              }}
            >
              <h3 className="text-sm font-medium text-vk-text-primary md:col-span-3">
                Registrar pago (un mes)
              </h3>
              <select
                aria-label="Tipo de pago"
                value={pago.expected}
                onChange={(e) =>
                  setPago({ ...pago, expected: e.target.value as "activate" | "renew" })
                }
                className={inputClass}
              >
                <option value="activate">Activar (primer pago)</option>
                <option value="renew">Renovar</option>
              </select>
              <input
                aria-label="Referencia del pago"
                placeholder="Referencia (ej. TRF-0001)"
                value={pago.reference}
                onChange={(e) => setPago({ ...pago, reference: e.target.value })}
                className={inputClass}
              />
              <input
                aria-label="Plan"
                placeholder="Plan (obligatorio al activar)"
                list="planes"
                value={pago.plan_code}
                onChange={(e) => setPago({ ...pago, plan_code: e.target.value })}
                className={inputClass}
              />
              <input
                aria-label="Importe en USD"
                placeholder="Importe USD"
                inputMode="decimal"
                value={pago.amount_usd}
                onChange={(e) => setPago({ ...pago, amount_usd: e.target.value })}
                className={inputClass}
              />
              <input
                aria-label="Importe en ARS"
                placeholder="Importe ARS"
                inputMode="decimal"
                value={pago.amount_ars}
                onChange={(e) => setPago({ ...pago, amount_ars: e.target.value })}
                className={inputClass}
              />
              <input
                aria-label="Notas"
                placeholder="Notas (opcional)"
                value={pago.notes}
                onChange={(e) => setPago({ ...pago, notes: e.target.value })}
                className={inputClass}
              />
              <datalist id="planes">
                {PLANES.map((p) => (
                  <option key={p} value={p} />
                ))}
              </datalist>
              <div className="md:col-span-3">
                <ErrorText mensaje={registrarPago.isError ? adminErrorMessage(registrarPago.error) : null} />
                <button
                  type="submit"
                  disabled={
                    registrarPago.isPending ||
                    pago.reference.trim().length < 3 ||
                    !pago.amount_usd ||
                    !pago.amount_ars
                  }
                  className={buttonClass}
                >
                  Acreditar pago
                </button>
              </div>
            </form>

            <form
              className="flex flex-wrap items-end gap-3"
              onSubmit={(e) => {
                e.preventDefault();
                if (plan.trim()) cambiarPlan.mutate();
              }}
            >
              <div className="flex flex-col gap-1">
                <label htmlFor="plan-prox" className="text-xs text-vk-text-muted">
                  Plan de la próxima renovación
                </label>
                <input
                  id="plan-prox"
                  list="planes"
                  value={plan}
                  onChange={(e) => setPlan(e.target.value)}
                  className={inputClass}
                />
              </div>
              <button type="submit" disabled={!plan.trim() || cambiarPlan.isPending} className={buttonClass}>
                Programar cambio
              </button>
              <ErrorText mensaje={cambiarPlan.isError ? adminErrorMessage(cambiarPlan.error) : null} />
            </form>

            <div className="flex flex-col gap-2">
              {!confirmaCancelar ? (
                <div>
                  <button
                    type="button"
                    onClick={() => setConfirmaCancelar(true)}
                    className="rounded-md border border-vk-danger px-3 py-2 text-sm text-vk-danger"
                  >
                    Cancelar renovación…
                  </button>
                </div>
              ) : (
                <>
                  <p className="text-sm text-vk-text-secondary">
                    Cancela la renovación: el negocio conserva el acceso hasta el fin de lo ya
                    pagado y después se bloquea.
                  </p>
                  <input
                    aria-label="Motivo de la cancelación"
                    placeholder="Motivo (opcional)"
                    value={motivoCancelar}
                    onChange={(e) => setMotivoCancelar(e.target.value)}
                    className={inputClass}
                  />
                  <ErrorText mensaje={cancelar.isError ? adminErrorMessage(cancelar.error) : null} />
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => cancelar.mutate()}
                      disabled={cancelar.isPending}
                      className={dangerClass}
                    >
                      Confirmar cancelación
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmaCancelar(false)}
                      className="rounded-md border border-vk-border-w px-3 py-2 text-sm"
                    >
                      Volver
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        )}
      </Panel>

      <Panel title="Pagos acreditados">
        {(pagos.data ?? []).length === 0 ? (
          <p className="text-sm text-vk-text-muted">Sin pagos registrados.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-vk-text-muted">
              <tr>
                <th className="py-1">Referencia</th>
                <th>Tipo</th>
                <th>Plan</th>
                <th>USD</th>
                <th>ARS</th>
                <th>Período</th>
                <th>Operador</th>
              </tr>
            </thead>
            <tbody>
              {(pagos.data ?? []).map((p) => (
                <tr key={p.id} className="border-t border-vk-border-w">
                  <td className="py-1">{p.reference}</td>
                  <td>{p.kind}</td>
                  <td>{p.plan_code}</td>
                  <td>{p.amount_usd}</td>
                  <td>{p.amount_ars}</td>
                  <td>
                    {fechaCorta(p.period_start)} → {fechaCorta(p.period_end)}
                  </td>
                  <td className="text-xs text-vk-text-muted">{p.operator}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title="Usuarios">
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-vk-text-muted">
            <tr>
              <th className="py-1">Email</th>
              <th>Nombre</th>
              <th>Rol</th>
              <th>Activo</th>
              <th>Último ingreso</th>
            </tr>
          </thead>
          <tbody>
            {t.users.map((u) => (
              <tr key={u.user_id} className="border-t border-vk-border-w">
                <td className="py-1">{u.email}</td>
                <td>{u.full_name}</td>
                <td>{u.role_code}</td>
                <td>{u.is_active ? "Sí" : "No"}</td>
                <td>{fechaCorta(u.last_login_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Eventos recientes">
        {t.recent_events.length === 0 ? (
          <p className="text-sm text-vk-text-muted">Sin eventos.</p>
        ) : (
          <ul className="flex flex-col gap-2 text-sm">
            {t.recent_events.map((e, i) => (
              <li key={`${e.created_at}-${i}`} className="flex flex-col">
                <span className="text-vk-text-primary">{e.decision_type}</span>
                <span className="text-xs text-vk-text-muted">{fechaYHora(e.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </PageWrapper>
  );
}
