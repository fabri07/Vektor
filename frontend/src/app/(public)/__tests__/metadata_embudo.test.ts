/**
 * Cada pantalla del embudo tiene `<title>` propio.
 *
 * Tres de las cuatro no podían tenerlo: arrancaban con `"use client"` en la
 * línea 1 y Next prohíbe exportar `metadata` desde un Client Component, así
 * que heredaban el título genérico del layout raíz. Un visitante con tres
 * pestañas abiertas veía "Véktor" tres veces, y el resultado de buscar
 * "solicitud véktor" en el historial no distinguía nada.
 *
 * El arreglo fue bajar el `"use client"` un nivel, del `page.tsx` al
 * componente con estado. Este test es lo que evita que alguien lo suba de
 * vuelta sin darse cuenta: si el `page.tsx` vuelve a ser cliente, el `export
 * const metadata` desaparece y esto se pone en rojo.
 */

import { metadata as solicitarAcceso } from "../solicitar-acceso/page";
import { metadata as solicitudEnviada } from "../solicitud-enviada/page";
import { metadata as solicitudVerificada } from "../solicitud-verificada/page";
import { metadata as definirPassword } from "../definir-password/page";
import { metadata as login } from "../login/page";

const PANTALLAS = [
  ["/solicitar-acceso", solicitarAcceso],
  ["/solicitud-enviada", solicitudEnviada],
  ["/solicitud-verificada", solicitudVerificada],
  ["/definir-password", definirPassword],
  ["/login", login],
] as const;

describe("metadata del embudo de acceso", () => {
  test.each(PANTALLAS)("%s declara título y descripción propios", (_ruta, meta) => {
    expect(typeof meta.title).toBe("string");
    expect(meta.title).toContain("Véktor");
    expect(typeof meta.description).toBe("string");
    expect((meta.description as string).length).toBeGreaterThan(20);
  });

  test("ningún título se repite: son cinco pantallas distintas", () => {
    const titulos = PANTALLAS.map(([, meta]) => meta.title);
    expect(new Set(titulos).size).toBe(PANTALLAS.length);
  });
});
