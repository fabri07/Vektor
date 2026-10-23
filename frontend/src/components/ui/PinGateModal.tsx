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
 * - «¿Olvidaste tu PIN?» (o `openReset`) lo restablece con la contraseña de la
 *   cuenta, abre la ventana con el PIN nuevo y, si había una acción esperando,
 *   la retoma (una sola vez: `_pinRetry` en el interceptor).
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
  // PIN olvidado: se restablece con la contraseña de la cuenta.
  const [olvido, setOlvido] = useState(false);
  const [password, setPassword] = useState("");
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
      setOlvido(false);
      setPassword("");
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

  const isReset = mode === "reset" || olvido;
  const isSetup = !isReset && (mode === "setup" || forceSetup);
  // Los dos piden el PIN dos veces.
  const pideRepetir = isSetup || isReset;

  async function handleSubmit() {
    setError(null);
    if (!PIN_RE.test(pin)) {
      setError("El PIN debe tener 4 dígitos numéricos.");
      return;
    }
    if (pideRepetir && pin !== pinConfirm) {
      setError("Los PIN no coinciden.");
      return;
    }
    if (isReset && !password) {
      setError("Escribí la contraseña de tu cuenta.");
      return;
    }
    setLoading(true);
    try {
      if (isReset) {
        await securityService.resetPin(password, pin, pinConfirm);
        // El reset cierra la ventana: se abre con el PIN nuevo, y así la
        // acción que estaba esperando puede seguir.
        await securityService.verifyPin(pin);
        addToast("PIN restablecido.", "success");
      } else if (isSetup) {
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
      title={
        isReset
          ? "Restablecé tu PIN"
          : isSetup
            ? "Configurá tu PIN de seguridad"
            : "Ingresá tu PIN"
      }
      size="sm"
    >
      <div className="space-y-4">
        <p className="text-sm text-vk-text-secondary">
          {isReset
            ? "Con la contraseña de tu cuenta elegís un PIN nuevo de 4 dígitos."
            : isSetup
              ? "Elegí un PIN de 4 dígitos. Te lo vamos a pedir antes de modificar datos sensibles."
              : "Esta acción requiere tu PIN de 4 dígitos."}
        </p>

        {isReset && (
          <div>
            <label htmlFor="pin-reset-password" className="mb-1 block text-xs text-vk-text-muted">
              Contraseña de tu cuenta
            </label>
            <input
              id="pin-reset-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-lg border border-vektor-border bg-vektor-night px-3 py-2 text-vektor-white focus:border-vektor-blue focus:outline-none"
            />
          </div>
        )}

        <div>
          <label htmlFor="pin-gate-pin" className="mb-1 block text-xs text-vk-text-muted">
            {isReset ? "PIN nuevo" : "PIN"}
          </label>
          <input
            id="pin-gate-pin"
            ref={inputRef}
            type="password"
            inputMode="numeric"
            autoComplete="off"
            maxLength={4}
            value={pin}
            onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 4))}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !pideRepetir) handleSubmit();
            }}
            className="w-full rounded-lg border border-vektor-border bg-vektor-night px-3 py-2 text-center text-2xl tracking-[0.5em] text-vektor-white focus:border-vektor-blue focus:outline-none"
            placeholder="••••"
          />
        </div>

        {pideRepetir && (
          <div>
            <label htmlFor="pin-gate-confirm" className="mb-1 block text-xs text-vk-text-muted">
              Repetí el PIN
            </label>
            <input
              id="pin-gate-confirm"
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

        {!isSetup && !isReset && (
          <button
            type="button"
            className="text-xs text-vk-text-muted underline hover:text-vk-text-secondary"
            onClick={() => {
              setOlvido(true);
              setPin("");
              setPinConfirm("");
              setError(null);
            }}
          >
            ¿Olvidaste tu PIN?
          </button>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={cancel} disabled={loading}>
            Cancelar
          </Button>
          <Button onClick={handleSubmit} loading={loading}>
            {isReset ? "Restablecer PIN" : isSetup ? "Guardar PIN" : "Confirmar"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
