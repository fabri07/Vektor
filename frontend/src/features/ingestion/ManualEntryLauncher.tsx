"use client";

import { useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ManualEntrySection } from "./ManualEntrySection";
import { useOfflineQueueCount, useOfflineSubmit } from "./useOfflineSubmit";

interface ManualEntryLauncherProps {
  variant?: "primary" | "secondary";
}

/**
 * Botón que abre la carga manual en un Modal flotante. Se usa en /ingestion y en
 * los headers de /sales, /expenses, /products (slot `actions` de PageWrapper).
 * Monta una vez por página: dueño del autosync de la cola offline + badge de pendientes.
 */
export function ManualEntryLauncher({ variant = "secondary" }: ManualEntryLauncherProps) {
  const [open, setOpen] = useState(false);
  const dirtyRef = useRef(false);
  useOfflineSubmit({ autoSync: true });
  const pending = useOfflineQueueCount();

  // Cierre (botón / Escape / overlay → todos pasan por onClose): si hay datos sin
  // guardar, confirmar antes de descartar.
  function handleClose() {
    if (
      dirtyRef.current &&
      !window.confirm("Tenés datos sin guardar en este formulario. ¿Cerrar y descartarlos?")
    ) {
      return;
    }
    dirtyRef.current = false;
    setOpen(false);
  }

  return (
    <>
      <Button type="button" size="sm" variant={variant} onClick={() => setOpen(true)}>
        + Cargar datos manualmente
        {pending > 0 && (
          <span
            className="ml-1.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-vk-blue px-1.5 text-[11px] font-semibold text-vektor-white"
            title={`${pending} carga(s) pendientes de sincronizar`}
          >
            {pending}
          </span>
        )}
      </Button>
      <Modal
        isOpen={open}
        onClose={handleClose}
        title="Carga manual de datos"
        size="4xl"
      >
        <ManualEntrySection onDirtyChange={(d) => (dirtyRef.current = d)} />
      </Modal>
    </>
  );
}
