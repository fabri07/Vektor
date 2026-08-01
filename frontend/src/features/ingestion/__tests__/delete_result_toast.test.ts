/**
 * El resultado del borrado distingue "se revirtió todo" de "quedaron cosas".
 *
 * El endpoint dejó de responder 204 mudo justamente para esto: decir "datos
 * revertidos" cuando sobrevivieron entidades sería la promesa que este trabajo
 * elimina.
 */

import type { FileDeletionResult } from "@/services/ingestion.service";

/** Espeja la decisión del `onSuccess` de FileListSection. */
function mensajeDeBorrado(resultado: FileDeletionResult | undefined): string {
  const conservados = resultado?.conservados?.length ?? 0;
  if (!resultado || resultado.fully_reverted) {
    return "Archivo eliminado y datos revertidos.";
  }
  return `Archivo eliminado. Se conservaron ${conservados} ${
    conservados === 1 ? "registro" : "registros"
  } con actividad posterior — revisalos.`;
}

const base: FileDeletionResult = {
  status: "deleted",
  fully_reverted: true,
  deleted: { sales: 3, expenses: 1, products: 2 },
  restored: { products: 0, masters: 0 },
  conservados: [],
};

describe("mensaje del borrado", () => {
  it("afirma la reversión completa sólo cuando lo fue", () => {
    expect(mensajeDeBorrado(base)).toBe("Archivo eliminado y datos revertidos.");
  });

  it("no afirma reversión completa si quedaron entidades", () => {
    const parcial: FileDeletionResult = {
      ...base,
      fully_reverted: false,
      conservados: [
        {
          entity_type: "product",
          id: "p1",
          name: "Vela aroma 200g",
          reasons: ["venta_manual_posterior"],
          fields: [],
        },
      ],
    };
    const msg = mensajeDeBorrado(parcial);
    expect(msg).not.toContain("datos revertidos");
    expect(msg).toContain("1 registro");
    expect(msg).toContain("revisalos");
  });

  it("concuerda el plural", () => {
    const dos: FileDeletionResult = {
      ...base,
      fully_reverted: false,
      conservados: [
        { entity_type: "product", id: "a", name: "A", reasons: ["compra_posterior"], fields: [] },
        { entity_type: "customer", id: "b", name: "B", reasons: ["venta_manual_posterior"], fields: [] },
      ],
    };
    expect(mensajeDeBorrado(dos)).toContain("2 registros");
  });

  it("una respuesta vieja sin cuerpo no rompe la pantalla", () => {
    expect(mensajeDeBorrado(undefined)).toBe("Archivo eliminado y datos revertidos.");
  });
});
