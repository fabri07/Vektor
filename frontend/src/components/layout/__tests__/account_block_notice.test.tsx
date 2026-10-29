import "@testing-library/jest-dom";
import React from "react";
import { act, render, screen } from "@testing-library/react";

import { AccountBlockNotice } from "@/components/layout/AccountBlockNotice";
import { bloqueoDeCodigo, useAccountStatusStore } from "@/stores/accountStatusStore";

beforeEach(() => {
  act(() => useAccountStatusStore.getState().clear());
});

describe("AccountBlockNotice", () => {
  it("no muestra nada si la cuenta no está bloqueada", () => {
    const { container } = render(<AccountBlockNotice />);
    expect(container).toBeEmptyDOMElement();
  });

  it("una cuenta suspendida tapa la pantalla con el mensaje del servidor", () => {
    act(() => useAccountStatusStore.getState().setBlock("SUSPENDED", "Mensaje del soporte"));
    render(<AccountBlockNotice />);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(screen.getByText("Mensaje del soporte")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cerrar sesión/i })).toBeInTheDocument();
  });

  it("en la caja una cuenta suspendida es una franja: la cola de ventas sigue a la vista", () => {
    act(() => useAccountStatusStore.getState().setBlock("SUSPENDED"));
    render(<AccountBlockNotice enCaja />);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/suspendida/i);
  });

  it("solo lectura es una franja, no una pantalla", () => {
    act(() => useAccountStatusStore.getState().setBlock("READ_ONLY"));
    render(<AccountBlockNotice />);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/solo lectura/i);
  });
});

describe("bloqueoDeCodigo", () => {
  it("solo reconoce los dos códigos de cuenta, no los de suscripción", () => {
    expect(bloqueoDeCodigo("TENANT_SUSPENDED")).toBe("SUSPENDED");
    expect(bloqueoDeCodigo("TENANT_READ_ONLY")).toBe("READ_ONLY");
    expect(bloqueoDeCodigo("SUBSCRIPTION_READ_ONLY")).toBeNull();
    expect(bloqueoDeCodigo(undefined)).toBeNull();
  });
});
