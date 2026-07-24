import "@testing-library/jest-dom";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";

import { MasterPreviewPanel } from "../MasterPreviewPanel";
import type { MasterPreviewSummary } from "@/services/ingestion.service";

const CUSTOMER_PREVIEW: MasterPreviewSummary = {
  context_id: "hoja-clientes",
  entity_type: "customer",
  to_create: 3,
  to_update: 1,
  needs_review: 2,
  invalid: 1,
  duplicates: 1,
  samples: [
    {
      row_index: 2,
      status: "needs_review",
      display_name: "Juan Pérez",
      existing_name: null,
      issue: "Sin documento ni email para identificar",
    },
    {
      row_index: 5,
      status: "invalid",
      display_name: "María",
      existing_name: null,
      issue: "CUIT inválido",
    },
    {
      row_index: 8,
      status: "duplicate_in_file",
      display_name: "Pedro López",
      existing_name: "Pedro López",
      issue: null,
    },
    // create/update no van en la lista "navegable" — solo needs_review/invalid/duplicate.
    { row_index: 0, status: "create", display_name: "Nuevo Cliente", existing_name: null, issue: null },
  ],
};

describe("MasterPreviewPanel — F7e", () => {
  test("no renderiza nada si no hay previews", () => {
    const { container } = render(<MasterPreviewPanel previews={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  test("muestra el label de la entidad y los conteos por bucket", () => {
    render(<MasterPreviewPanel previews={[CUSTOMER_PREVIEW]} />);

    expect(screen.getByText("Clientes")).toBeInTheDocument();
    expect(screen.getByText("Se crean")).toBeInTheDocument();
    expect(screen.getByText("Se actualizan")).toBeInTheDocument();
    expect(screen.getByText("En revisión")).toBeInTheDocument();
    expect(screen.getByText("Inválidos")).toBeInTheDocument();
    expect(screen.getByText("Duplicados")).toBeInTheDocument();
  });

  test("aclara que solo se importan create/update — needs_review/invalid no es un fallo", () => {
    render(<MasterPreviewPanel previews={[CUSTOMER_PREVIEW]} />);

    expect(
      screen.getByText(/Solo se importan los registros a crear\/actualizar/i),
    ).toBeInTheDocument();
    // No debe usar lenguaje de error/fallo.
    expect(screen.queryByText(/fall[oó]/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/error/i)).not.toBeInTheDocument();
  });

  test("las muestras needs_review/invalid/duplicate son navegables (disclosure) y muestran el motivo", () => {
    render(<MasterPreviewPanel previews={[CUSTOMER_PREVIEW]} />);

    // El detalle vive detrás de un <details>/<summary> — colapsado por default.
    const disclosure = screen.getByText(/Ver por qué/i).closest("details");
    expect(disclosure).not.toBeNull();
    expect(disclosure).not.toHaveAttribute("open");

    fireEvent.click(screen.getByText(/Ver por qué/i));

    expect(screen.getByText(/Juan Pérez/)).toBeInTheDocument();
    expect(screen.getByText(/Sin documento ni email para identificar/)).toBeInTheDocument();
    expect(screen.getByText(/María/)).toBeInTheDocument();
    expect(screen.getByText(/CUIT inválido/)).toBeInTheDocument();
    // Aparece dos veces: como display_name y como existing_name ("existente: ...").
    expect(screen.getAllByText(/Pedro López/).length).toBe(2);
    // La fila "create" no aparece en la lista navegable (ya se importa, no hace falta explicarla).
    expect(screen.queryByText("Nuevo Cliente")).not.toBeInTheDocument();
  });

  test("proveedores usa su propio label", () => {
    render(
      <MasterPreviewPanel
        previews={[{ ...CUSTOMER_PREVIEW, entity_type: "supplier", context_id: "hoja-proveedores" }]}
      />,
    );
    expect(screen.getByText("Proveedores")).toBeInTheDocument();
  });
});
