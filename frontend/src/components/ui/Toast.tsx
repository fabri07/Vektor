"use client";

import { useEffect, useState } from "react";
import { useToastStore, type ToastItem, type ToastVariant } from "@/stores/toastStore";

// ── Estilos por variante ──────────────────────────────────────────────────────

const VARIANT_STYLES: Record<ToastVariant, { wrapper: string; icon: JSX.Element }> = {
  success: {
    wrapper: "border-vk-success/30 bg-vk-success-bg text-vk-success",
    icon: (
      <svg className="h-4 w-4 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
      </svg>
    ),
  },
  error: {
    wrapper: "border-vk-danger/30 bg-vk-danger-bg text-vk-danger",
    icon: (
      <svg className="h-4 w-4 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    ),
  },
  info: {
    wrapper: "border-vk-info/30 bg-vk-info-bg text-vk-info",
    icon: (
      <svg className="h-4 w-4 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />
      </svg>
    ),
  },
  warning: {
    wrapper: "border-vk-warning/30 bg-vk-warning-bg text-vk-warning",
    icon: (
      <svg className="h-4 w-4 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
      </svg>
    ),
  },
};

// ── Toast individual ──────────────────────────────────────────────────────────

const DISMISS_DELAY = 4000;
const EXIT_DURATION = 300;

function ToastMessage({ toast }: { toast: ToastItem }) {
  const remove = useToastStore((s) => s.remove);
  const [visible, setVisible] = useState(false);

  // Slide-in al montar
  useEffect(() => {
    const id = requestAnimationFrame(() => setVisible(true));
    return () => cancelAnimationFrame(id);
  }, []);

  // Auto-dismiss
  useEffect(() => {
    const timer = setTimeout(() => handleDismiss(), DISMISS_DELAY);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleDismiss() {
    setVisible(false);
    setTimeout(() => remove(toast.id), EXIT_DURATION);
  }

  const styles = VARIANT_STYLES[toast.variant];
  const isAlert = toast.variant === "error" || toast.variant === "warning";

  return (
    <div
      role={isAlert ? "alert" : "status"}
      aria-live={isAlert ? "assertive" : "polite"}
      className={[
        "flex w-80 items-start gap-3 rounded-lg border px-4 py-3 text-sm font-medium shadow-vk-md",
        "transition-all",
        styles.wrapper,
        visible
          ? "translate-y-0 opacity-100"
          : "translate-y-3 opacity-0",
      ]
        .filter(Boolean)
        .join(" ")}
      style={{ transitionDuration: `${EXIT_DURATION}ms` }}
    >
      {styles.icon}
      <span className="flex-1 leading-snug">{toast.message}</span>
      <button
        type="button"
        onClick={handleDismiss}
        aria-label="Cerrar notificación"
        className="flex-shrink-0 opacity-60 hover:opacity-100 focus:outline-none"
      >
        <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  );
}

// ── Contenedor global ─────────────────────────────────────────────────────────

export function ToastContainer() {
  const toasts = useToastStore((s) => s.toasts);

  return (
    <div
      aria-label="Notificaciones"
      className="fixed bottom-6 right-6 z-[9999] flex flex-col gap-2"
    >
      {toasts.map((toast) => (
        <ToastMessage key={toast.id} toast={toast} />
      ))}
    </div>
  );
}

// ── Hook de conveniencia ──────────────────────────────────────────────────────

export function useToast() {
  const add = useToastStore((s) => s.add);
  return {
    success: (message: string) => add(message, "success"),
    error: (message: string) => add(message, "error"),
    info: (message: string) => add(message, "info"),
    warning: (message: string) => add(message, "warning"),
  };
}
