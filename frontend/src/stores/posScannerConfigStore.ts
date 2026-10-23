import { create } from "zustand";
import { persist } from "zustand/middleware";

/**
 * Cómo emite el lector de ESTA PC (B13). Por PC y no por usuario: depende del
 * aparato. Hay lectores que terminan con Tab en vez de Enter, y los Bluetooth
 * o los configurados con retardo mandan las teclas más espaciadas.
 */
export type ScannerTerminator = "Enter" | "Tab";

export const DEFAULT_THRESHOLD_MS = 30;
export const MIN_SCAN_LENGTH = 4;

interface ScannerConfigState {
  terminator: ScannerTerminator;
  thresholdMs: number;
  setTerminator: (t: ScannerTerminator) => void;
  setThresholdMs: (ms: number) => void;
}

export const usePosScannerConfigStore = create<ScannerConfigState>()(
  persist(
    (set) => ({
      terminator: "Enter",
      thresholdMs: DEFAULT_THRESHOLD_MS,
      setTerminator: (terminator) => set({ terminator }),
      setThresholdMs: (ms) => set({ thresholdMs: Math.min(200, Math.max(10, Math.round(ms))) }),
    }),
    { name: "vektor-pos-scanner" },
  ),
);
