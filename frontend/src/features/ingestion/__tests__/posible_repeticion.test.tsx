import { fireEvent, render, screen } from "@testing-library/react";

import type { ImportacionResponse } from "@/services/ingestion.service";

import { PosibleRepeticionAviso } from "../PosibleRepeticionAviso";
import { repeticionDelConfirm, repeticionDelIntento } from "../posibleRepeticion";

function error409(detail: unknown) {
  return { response: { status: 409, data: { detail } } };
}

describe("repeticionDelConfirm", () => {
  it("reconoce el 409 de repetición masiva por su código", () => {
    expect(
      repeticionDelConfirm(error409({ code: "POSIBLE_REPETICION", message: "12 de 12 coinciden" })),
    ).toBe("12 de 12 coinciden");
  });

  it("no confunde un 409 de import en curso con una repetición", () => {
    expect(repeticionDelConfirm(error409("Ya se está importando"))).toBeNull();
    expect(repeticionDelConfirm(error409({ code: "FILE_REVISION_CHANGED" }))).toBeNull();
  });

  it("ignora otros status", () => {
    expect(
      repeticionDelConfirm({ response: { status: 422, data: { detail: { code: "POSIBLE_REPETICION" } } } }),
    ).toBeNull();
  });
});

describe("repeticionDelIntento", () => {
  const base = {
    attempt_id: "a",
    file_id: "f",
    phase: null,
    rows_total: null,
    rows_done: 0,
    result: null,
    created_at: "",
    finished_at: null,
  };

  it("reconoce un intento asíncrono frenado por repetición", () => {
    const imp: ImportacionResponse = {
      ...base,
      status: "FALLADO",
      error_code: "posible_repeticion",
      error_detail: "parece repetida",
    };
    expect(repeticionDelIntento(imp)).toBe("parece repetida");
  });

  it("no toma otros fallos", () => {
    const imp: ImportacionResponse = {
      ...base,
      status: "FALLADO",
      error_code: "validacion",
      error_detail: "x",
    };
    expect(repeticionDelIntento(imp)).toBeNull();
    expect(repeticionDelIntento(null)).toBeNull();
  });
});

describe("PosibleRepeticionAviso", () => {
  it("no deja importar hasta marcar que se sabe", () => {
    const onImportarIgual = jest.fn();
    render(
      <PosibleRepeticionAviso
        mensaje="12 de las 12 operaciones coinciden"
        enviando={false}
        onImportarIgual={onImportarIgual}
      />,
    );
    const boton = screen.getByRole("button", { name: "Importar igual" });
    expect(boton).toBeDisabled();
    fireEvent.click(boton);
    expect(onImportarIgual).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("checkbox"));
    expect(boton).toBeEnabled();
    fireEvent.click(boton);
    expect(onImportarIgual).toHaveBeenCalledTimes(1);
    expect(screen.getByText("12 de las 12 operaciones coinciden")).toBeInTheDocument();
  });
});
