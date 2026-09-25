"use client";

import { useEffect, useRef } from "react";

import { MIN_SCAN_LENGTH, type ScannerTerminator } from "@/stores/posScannerConfigStore";

/**
 * Lector de códigos USB en modo teclado (B13).
 *
 * El lector "tipea" el código entero en unos pocos milisegundos y termina con
 * Enter (o Tab). Lo que distingue una lectura de una persona es la RÁFAGA:
 * todas las teclas separadas por menos de `thresholdMs`, al menos
 * `MIN_SCAN_LENGTH` caracteres, y el terminador.
 *
 * Dónde se escucha, según el elemento con foco:
 * - Nada editable (la pantalla, un botón): se escucha. El terminador se
 *   CONSUME: sin eso, el Enter de una lectura "aprieta" el botón con foco
 *   —p. ej. Cobrar— y el Tab mueve el foco.
 * - Un input marcado `data-pos-scan="capture"` (los numéricos de la caja): se
 *   escucha igual. Los caracteres de la ráfaga ya entraron al input antes de
 *   saber que eran una lectura, así que al detectarla se RESTAURA el valor que
 *   tenía antes del primero. Si no, un escaneo con el foco en "efectivo
 *   recibido" metía el código en el importe.
 * - Cualquier otro campo de texto (el buscador por nombre, el motivo): se
 *   ignora. Ahí escribe una persona.
 *
 * Sin dependencias. Escucha en fase de CAPTURA sobre `window`, para ver la
 * tecla antes que el elemento con foco.
 */

export interface UseScannerOptions {
  onScan: (code: string) => void;
  enabled: boolean;
  terminator: ScannerTerminator;
  thresholdMs: number;
  /** Inyectable para los tests; por defecto `performance.now()`. */
  now?: () => number;
}

type Editable = HTMLInputElement | HTMLTextAreaElement;

function editableOf(target: EventTarget | null): Editable | "contenteditable" | null {
  if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) return target;
  if (target instanceof HTMLElement && target.isContentEditable) return "contenteditable";
  return null;
}

function isCaptureInput(el: Editable): boolean {
  return el.getAttribute("data-pos-scan") === "capture";
}

/**
 * Pone el valor de un input controlado por React. Asignar `.value` a secas no
 * avisa a React: el estado seguiría teniendo el código tipeado.
 */
function restoreValue(el: Editable, value: string): void {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement : HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(proto.prototype, "value")?.set;
  if (setter) setter.call(el, value);
  else el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

const MODIFIERS = new Set(["Shift", "CapsLock", "Control", "Alt", "Meta", "AltGraph"]);

export function useScanner({
  onScan,
  enabled,
  terminator,
  thresholdMs,
  now,
}: UseScannerOptions): void {
  // En refs: el listener se registra una sola vez y lee siempre lo último.
  const onScanRef = useRef(onScan);
  onScanRef.current = onScan;
  const config = useRef({ enabled, terminator, thresholdMs, now });
  config.current = { enabled, terminator, thresholdMs, now };

  useEffect(() => {
    let buffer = "";
    let last = -Infinity;
    // El input donde arrancó la ráfaga y el valor que tenía antes.
    let origin: Editable | null = null;
    let snapshot = "";

    function reset(): void {
      buffer = "";
      origin = null;
      snapshot = "";
    }

    function onKeyDown(e: KeyboardEvent): void {
      const { enabled: on, terminator: term, thresholdMs: ms, now: clock } = config.current;
      if (!on) {
        reset();
        return;
      }
      if (MODIFIERS.has(e.key)) return; // El lector usa Shift para las mayúsculas.

      const editable = editableOf(e.target);
      if (editable === "contenteditable" || (editable && !isCaptureInput(editable))) {
        reset();
        return;
      }

      const t = (clock ?? (() => performance.now()))();
      const enRafaga = t - last <= ms;

      if (e.key === term) {
        if (enRafaga && buffer.length >= MIN_SCAN_LENGTH) {
          e.preventDefault();
          e.stopPropagation();
          const code = buffer;
          if (origin) restoreValue(origin, snapshot);
          reset();
          last = -Infinity;
          onScanRef.current(code);
          return;
        }
        reset();
        last = -Infinity;
        return;
      }

      if (e.key.length !== 1 || e.ctrlKey || e.metaKey || e.altKey) {
        reset();
        last = -Infinity;
        return;
      }

      if (!enRafaga || buffer === "") {
        buffer = e.key;
        origin = editable;
        // keydown llega ANTES de que el carácter entre al input.
        snapshot = editable ? editable.value : "";
      } else {
        buffer += e.key;
      }
      last = t;
    }

    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, []);
}
