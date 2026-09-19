import "@testing-library/jest-dom";
import React from "react";
import { render, screen } from "@testing-library/react";

import PreciosPage from "../page";

/**
 * Los CTAs de /precios son el único lugar donde el visitante declara con qué
 * plan quiere arrancar ANTES de ver el formulario. El `?plan=` que llevan es lo
 * que precarga la elección; que esa precarga funcione y quede editable lo
 * verifica `features/access-request/__tests__/access_request_form.test.tsx`.
 */
describe("/precios — CTAs de solicitud", () => {
  test.each([
    ["Esencial", "esencial"],
    ["Control", "control"],
    ["Dirección", "direccion"],
  ])("el card %s lleva al formulario con plan=%s", (nombre, codigo) => {
    render(<PreciosPage />);
    const cta = screen.getByRole("link", {
      name: new RegExp(`Quiero pedir el plan ${nombre}`, "i"),
    });
    expect(cta).toHaveAttribute(
      "href",
      `/solicitar-acceso?plan=${codigo}&src=precios_${codigo}`,
    );
  });

  test("el card Control lleva la marca de recomendado", () => {
    render(<PreciosPage />);
    expect(screen.getByText(/Recomendado/i)).toBeInTheDocument();
  });

  test("ningún CTA promete un alta inmediata ni apunta a /register", () => {
    const { container } = render(<PreciosPage />);
    const hrefs = Array.from(container.querySelectorAll("a")).map((a) =>
      a.getAttribute("href"),
    );
    expect(hrefs).not.toContain("/register");
    // "Empezar gratis" sobrepromete: pedir acceso es una postulación.
    expect(screen.queryByText(/Empezar gratis/i)).toBeNull();
  });
});
