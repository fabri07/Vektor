/**
 * `/rubros` cubre los seis rubros, y el nav no puede desincronizarse de ella.
 *
 * La duplicación que estos tests cierran era real: la lista del menú "Rubros"
 * del nav estaba escrita a mano en `PublicNav.tsx` y las secciones estaban
 * escritas a mano en la página. Sumar un rubro y olvidarse de uno de los dos
 * lados no rompía nada — dejaba un rubro sin entrada en el menú, o una entrada
 * apuntando a un ancla inexistente. Ninguna de las dos cosas falla sola.
 */

import "@testing-library/jest-dom";
import React from "react";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PublicNav } from "@/components/public/PublicNav";
import { RUBROS } from "@/lib/rubros";
import { VERTICAL_OPTIONS } from "@/lib/verticals";
import RubrosPage from "../page";

describe("/rubros — cobertura de los seis rubros", () => {
  test("hay una sección por vertical operativo, con el ancla que declara", () => {
    const { container } = render(<RubrosPage />);

    expect(VERTICAL_OPTIONS).toHaveLength(6);
    for (const { name, anchor } of VERTICAL_OPTIONS) {
      expect(container.querySelector(`section#${anchor}`)).not.toBeNull();
      expect(screen.getByRole("heading", { name, level: 2 })).toBeInTheDocument();
    }
  });

  test("el índice del encabezado enlaza a las seis anclas", () => {
    render(<RubrosPage />);
    const indice = screen.getByRole("navigation", { name: /ir a un rubro/i });

    for (const { name, anchor } of VERTICAL_OPTIONS) {
      expect(within(indice).getByRole("link", { name })).toHaveAttribute("href", `#${anchor}`);
    }
  });

  test("no hay rótulo decorativo arriba del título", () => {
    render(<RubrosPage />);
    // El `PARA TU RUBRO` que había no aportaba nada que el h1 no dijera mejor.
    expect(screen.queryByText(/para tu rubro/i)).not.toBeInTheDocument();
  });

  test("cada rubro declara exactamente tres capacidades", () => {
    // Tres es una decisión de diseño de la sección, no un accidente: con dos se
    // ve pobre y con cinco deja de leerse. Si un rubro necesita más, se cambia
    // el diseño de la sección, no se cuelga una capacidad de más.
    for (const rubro of RUBROS) {
      expect(rubro.capacidades).toHaveLength(3);
    }
  });
});

describe("PublicNav — el menú Rubros se deriva de la misma fuente", () => {
  test("cada vertical aparece en el menú apuntando a su sección", async () => {
    const user = userEvent.setup();
    render(<PublicNav />);
    await user.click(screen.getByRole("button", { name: /Soluciones por rubro/i }));

    for (const { name, anchor } of VERTICAL_OPTIONS) {
      expect(screen.getByRole("link", { name })).toHaveAttribute("href", `/rubros#${anchor}`);
    }
  });
});
