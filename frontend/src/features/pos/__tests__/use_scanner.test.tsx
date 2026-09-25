import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

import { useScanner } from "@/features/pos/useScanner";
import type { ScannerTerminator } from "@/stores/posScannerConfigStore";

let reloj = 0;
const onScan = jest.fn();

function Banco({ terminator = "Enter" as ScannerTerminator }) {
  const [monto, setMonto] = useState("");
  const [libre, setLibre] = useState("");
  useScanner({ onScan, enabled: true, terminator, thresholdMs: 30, now: () => reloj });
  return (
    <div>
      <button type="button">Cobrar</button>
      <input
        aria-label="monto"
        data-pos-scan="capture"
        value={monto}
        onChange={(e) => setMonto(e.target.value)}
      />
      <input aria-label="buscador" value={libre} onChange={(e) => setLibre(e.target.value)} />
    </div>
  );
}

/** Teclea como un lector (o una persona, con `gapMs` grande). Devuelve si el terminador pasó. */
function teclear(target: Element, texto: string, gapMs: number, term = "Enter"): boolean {
  for (const ch of texto) {
    reloj += gapMs;
    fireEvent.keyDown(target, { key: ch });
  }
  reloj += gapMs;
  return fireEvent.keyDown(target, { key: term });
}

beforeEach(() => {
  reloj = 1000;
  onScan.mockReset();
});

describe("useScanner", () => {
  it("una ráfaga es una lectura", () => {
    render(<Banco />);
    teclear(document.body, "7790001234567", 5);
    expect(onScan).toHaveBeenCalledWith("7790001234567");
  });

  it("una persona tipeando no es una lectura", () => {
    render(<Banco />);
    teclear(document.body, "7790001234567", 120);
    expect(onScan).not.toHaveBeenCalled();
  });

  it("dos teclas rápidas no alcanzan el mínimo", () => {
    render(<Banco />);
    teclear(document.body, "12", 5);
    expect(onScan).not.toHaveBeenCalled();
  });

  it("Shift entre caracteres (mayúsculas del lector) no corta la ráfaga", () => {
    render(<Banco />);
    for (const ch of "VKT-0123456789AB") {
      reloj += 3;
      if (ch >= "A" && ch <= "Z") fireEvent.keyDown(document.body, { key: "Shift" });
      fireEvent.keyDown(document.body, { key: ch });
    }
    reloj += 3;
    fireEvent.keyDown(document.body, { key: "Enter" });
    expect(onScan).toHaveBeenCalledWith("VKT-0123456789AB");
  });

  it("el Enter de la lectura no llega al botón con foco (no cobra por accidente)", () => {
    render(<Banco />);
    const boton = screen.getByRole("button", { name: "Cobrar" });
    boton.focus();
    const paso = teclear(boton, "7790001234567", 5);
    expect(paso).toBe(false); // preventDefault
    expect(onScan).toHaveBeenCalled();
  });

  it("un Enter humano sí pasa", () => {
    render(<Banco />);
    const boton = screen.getByRole("button", { name: "Cobrar" });
    reloj += 500;
    expect(fireEvent.keyDown(boton, { key: "Enter" })).toBe(true);
  });

  it("con terminador Tab, el Tab se consume y el Enter no dispara", () => {
    render(<Banco terminator="Tab" />);
    expect(teclear(document.body, "7790001234567", 5, "Tab")).toBe(false);
    expect(onScan).toHaveBeenCalledTimes(1);
    teclear(document.body, "7790001234567", 5, "Enter");
    expect(onScan).toHaveBeenCalledTimes(1);
  });

  it("una ráfaga dentro de un input `capture` restaura su valor y se procesa como lectura", () => {
    render(<Banco />);
    const monto = screen.getByLabelText("monto") as HTMLInputElement;
    fireEvent.change(monto, { target: { value: "1000" } });
    // El navegador inserta cada carácter después del keydown.
    for (const ch of "7790001234567") {
      reloj += 5;
      fireEvent.keyDown(monto, { key: ch });
      fireEvent.change(monto, { target: { value: monto.value + ch } });
    }
    reloj += 5;
    fireEvent.keyDown(monto, { key: "Enter" });
    expect(onScan).toHaveBeenCalledWith("7790001234567");
    expect(monto.value).toBe("1000");
  });

  it("en un campo de texto libre el lector no interviene", () => {
    render(<Banco />);
    const buscador = screen.getByLabelText("buscador");
    expect(teclear(buscador, "7790001234567", 5)).toBe(true);
    expect(onScan).not.toHaveBeenCalled();
  });
});
