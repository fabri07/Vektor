"use client";

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { usePinGateStore } from "@/stores/pinGateStore";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";
import { securityService } from "@/services/security.service";

function errorDetail(err: unknown, fallback: string): string {
  const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data
    ?.detail;
  return typeof detail === "string" && detail !== "PIN_REQUIRED" ? detail : fallback;
}

const PIN_RE = /^\d{4}$/;

/**
 * Modal global del PIN de seguridad. Se monta una vez en el layout protegido.
 * - Reacciona al store (`requirePin` desde el interceptor 428).
 * - En modo `setup` permite configurar el PIN por primera vez (doble ingreso).
 * - Al montarse, si `must_set` está activo, abre el setup proactivamente.
 */
export function PinGateModal() {
  const { isOpen, mode, succeed, cancel, openSetup } = usePinGateStore();
  const user = useAuthStore((s) => s.user);
  const addToast = useToastStore((s) => s.add);
  const queryClient = useQueryClient();

  const [pin, setPin] = useState("");
  const [pinConfirm, setPinConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // Si el usuario aún no configuró PIN, forzamos setup aunque se haya pedido verify
  // (evita el dead-end: 428 → verify sin PIN posible).
  const [forceSetup, setForceSetup] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // Al desmontar el modal (logout / salir del layout protegido), rechazamos
  // cualquier verificación pendiente para no dejar requests colgados esperando.
  useEffect(() => {
    return () => {
      usePinGateStore.getState().cancel();
    };
  }, []);

  // Primera sesión: si el usuario debe configurar PIN, abrir el setup.
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    securityService
      .pinStatus()
      .then((s) => {
        if (!cancelled && s.must_set) openSetup();
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [user, openSetup]);

  // Reset al abrir/cerrar. Al abrir en verify, chequeamos si hay PIN configurado:
  // si no, forzamos setup (evita pedir un PIN inexistente).
  useEffect(() => {
    if (isOpen) {
      setPin("");
      setPinConfirm("");
      setError(null);
      setForceSetup(false);
      setTimeout(() => inputRef.current?.focus(), 50);
      if (mode === "verify") {
        securityService
          .pinStatus()
          .then((s) => {
            if (!s.pin_set) setForceSetup(true);
          })
          .catch(() => {});
      }
    }
  }, [isOpen, mode]);

  const isSetup = mode === "setup" || forceSetup;

  async function handleSubmit() {
    setError(null);
    if (!PIN_RE.test(pin)) {
      setError("El PIN debe tener 4 dígitos numéricos.");
      return;
    }
    if (isSetup && pin !== pinConfirm) {
      setError("Los PIN no coinciden.");
      return;
    }
    setLoading(true);
    try {
      if (isSetup) {
        await securityService.setupPin(pin, pinConfirm);
        // Abrir la ventana inmediatamente tras configurarlo.
        await securityService.verifyPin(pin);
        addToast("PIN configurado.", "success");
      } else {
        await securityService.verifyPin(pin);
      }
      // Refrescar el estado del PIN (SecurityPanel lo lee vía ['pin-status']).
      await queryClient.invalidateQueries({ queryKey: ["pin-status"] });
      succeed();
    } catch (err) {
      setError(errorDetail(err, "No se pudo verificar el PIN."));
    } finally {
      setLoading(false);
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={cancel}
      title={isSetup ? "Configurá tu PIN de seguridad" : "Ingresá tu PIN"}
      size="sm"
    >
      <div className="space-y-4">
        <p className="text-sm text-vk-text-secondary">
          {isSetup
            ? "Elegí un PIN de 4 dígitos. Te lo vamos a pedir antes de modificar datos sensibles."
            : "Esta acción requiere tu PIN de 4 dígitos."}
        </p>

        <div>
          <label className="mb-1 block text-xs text-vk-text-muted">PIN</label>
          <input
            ref={inputRef}
            type="password"
            inputMode="numeric"
            autoComplete="off"
            maxLength={4}
            value={pin}
            onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 4))}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !isSetup) handleSubmit();
            }}
            className="w-full rounded-lg border border-vektor-border bg-vektor-night px-3 py-2 text-center text-2xl tracking-[0.5em] text-vektor-white focus:border-vektor-blue focus:outline-none"
            placeholder="••••"
          />
        </div>

        {isSetup && (
          <div>
            <label className="mb-1 block text-xs text-vk-text-muted">Repetí el PIN</label>
            <input
              type="password"
              inputMode="numeric"
              autoComplete="off"
              maxLength={4}
              value={pinConfirm}
              onChange={(e) =>
                setPinConfirm(e.target.value.replace(/\D/g, "").slice(0, 4))
              }
              onKeyDown={(e) => {
                if (e.key === "Enter") handleSubmit();
              }}
              className="w-full rounded-lg border border-vektor-border bg-vektor-night px-3 py-2 text-center text-2xl tracking-[0.5em] text-vektor-white focus:border-vektor-blue focus:outline-none"
              placeholder="••••"
            />
          </div>
        )}

        {error && <p className="text-sm text-vk-danger">{error}</p>}

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={cancel} disabled={loading}>
            Cancelar
          </Button>
          <Button onClick={handleSubmit} loading={loading}>
            {isSetup ? "Guardar PIN" : "Confirmar"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
