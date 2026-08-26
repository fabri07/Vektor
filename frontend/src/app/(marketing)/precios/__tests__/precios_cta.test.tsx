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
  test("el card Gratuito lleva al formulario con plan=free", () => {
    render(<PreciosPage />);
    const cta = screen.getByRole("link", { name: /Quiero pedir mi acceso gratuito/i });
    expect(cta).toHaveAttribute("href", "/solicitar-acceso?plan=free&src=precios_free");
  });

  test("el card Premium lleva al formulario con plan=premium", () => {
    render(<PreciosPage />);
    const cta = screen.getByRole("link", { name: /Quiero recibir novedades de Premium/i });
    expect(cta).toHaveAttribute(
      "href",
      "/solicitar-acceso?plan=premium&src=precios_premium",
    );
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
