import "@testing-library/jest-dom";
import React from "react";
import { render, screen, within } from "@testing-library/react";

import { TargetSelect } from "../TargetSelect";
import type {
  CrossFieldCatalogEntry,
  FieldCatalogEntry,
} from "@/services/ingestion.service";

const CAMPOS: FieldCatalogEntry[] = [
  { value: "name", label: "Nombre", single_value: true },
  { value: "sale_price_ars", label: "Precio de venta", single_value: true },
];

const CRUZADOS: CrossFieldCatalogEntry[] = [
  { value: "supplier:name", label: "Proveedor — Nombre", entity: "supplier" },
];

function renderSelect(props: Partial<React.ComponentProps<typeof TargetSelect>> = {}) {
  return render(
    <TargetSelect
      target=""
      onChange={jest.fn()}
      fields={CAMPOS}
      className=""
      {...props}
    />,
  );
}

describe("destinos en otra sección", () => {
  it("los ofrece agrupados, separados de los campos de la hoja", () => {
    renderSelect({ crossFields: CRUZADOS });

    // El caso real: la columna "Tienda" de un catálogo declarando el proveedor.
    const grupo = screen.getByRole("group", { name: "Otras secciones" });
    expect(
      within(grupo).getByRole("option", { name: "Proveedor — Nombre" }),
    ).toBeInTheDocument();
    // Y no se cuela entre los campos del producto: el `<optgroup>` es lo que
    // hace legible que el dato se guarda en OTRA sección.
    expect(within(grupo).queryByRole("option", { name: "Nombre" })).toBeNull();
  });

  it("un cruzado ya elegido no se muestra como «campo desconocido»", () => {
    // Sin esto, elegir "Proveedor — Nombre" agregaba una segunda `<option>` con
    // el texto `supplier:name (campo desconocido)` — el mismo fallo de «la
    // pantalla muestra algo distinto de lo que se va a enviar» que este
    // componente vino a cerrar.
    renderSelect({ crossFields: CRUZADOS, target: "supplier:name" });

    expect(screen.queryByText(/campo desconocido/i)).toBeNull();
    expect(screen.getByRole("combobox")).toHaveValue("supplier:name");
  });

  it("sin cruzados no dibuja el grupo", () => {
    renderSelect();

    expect(screen.queryByRole("group", { name: "Otras secciones" })).toBeNull();
  });

  it("un canónico desconocido sigue avisando", () => {
    // La red de seguridad no se pierde: un target que no está en NINGUNA de las
    // dos listas sigue mostrándose como desconocido.
    renderSelect({ crossFields: CRUZADOS, target: "campo_inventado" });

    expect(screen.getByText(/campo desconocido/i)).toBeInTheDocument();
  });
});
