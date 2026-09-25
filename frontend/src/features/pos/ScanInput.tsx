"use client";

import { forwardRef, useState } from "react";

interface ScanInputProps {
  onCode: (code: string) => void;
  disabled?: boolean;
}

/**
 * Campo para tipear un código a mano (etiqueta borrosa, lector sin pilas).
 *
 * Es `data-pos-scan="capture"`: si el lector dispara con el foco acá,
 * `useScanner` restaura el campo y procesa la lectura — y como consume el
 * Enter, el formulario no la manda una segunda vez.
 */
export const ScanInput = forwardRef<HTMLInputElement, ScanInputProps>(function ScanInput(
  { onCode, disabled },
  ref,
) {
  const [value, setValue] = useState("");
  return (
    <form
      className="flex gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        const code = value.trim();
        if (!code) return;
        setValue("");
        onCode(code);
      }}
    >
      <input
        ref={ref}
        autoFocus
        aria-label="Código de barras"
        data-pos-scan="capture"
        placeholder="Escaneá o tipeá el código y Enter"
        className="h-11 flex-1 rounded-xl border border-vk-border-w bg-vk-surface-w px-3 font-mono text-sm text-vk-text-primary focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
        value={value}
        disabled={disabled}
        onChange={(e) => setValue(e.target.value)}
      />
    </form>
  );
});
