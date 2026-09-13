import {
  borrarPreferenciasDeColumnas,
  guardarPreferenciasDeColumnas,
  leerPreferenciasDeColumnas,
} from "@/lib/columnPreferences";

const KEY = "tenant-1:user-1:products";

beforeEach(() => {
  window.localStorage.clear();
});

test("guarda y relee las preferencias tal cual", () => {
  guardarPreferenciasDeColumnas(KEY, { visible: ["name", "sku"], known: ["name", "sku", "cost"] });
  expect(leerPreferenciasDeColumnas(KEY)).toEqual({
    visible: ["name", "sku"],
    known: ["name", "sku", "cost"],
  });
});

test("sin nada guardado devuelve null", () => {
  expect(leerPreferenciasDeColumnas(KEY)).toBeNull();
});

test("un valor corrupto (no JSON) devuelve null en vez de explotar", () => {
  window.localStorage.setItem(`vektor:columns:v1:${KEY}`, "{no es json");
  expect(leerPreferenciasDeColumnas(KEY)).toBeNull();
});

test("una forma inesperada (falta known, o tipos mezclados) devuelve null", () => {
  window.localStorage.setItem(`vektor:columns:v1:${KEY}`, JSON.stringify({ visible: ["a"] }));
  expect(leerPreferenciasDeColumnas(KEY)).toBeNull();

  window.localStorage.setItem(
    `vektor:columns:v1:${KEY}`,
    JSON.stringify({ visible: [1, 2], known: [] }),
  );
  expect(leerPreferenciasDeColumnas(KEY)).toBeNull();
});

test("borrar deja la clave sin nada para releer", () => {
  guardarPreferenciasDeColumnas(KEY, { visible: ["name"], known: ["name"] });
  borrarPreferenciasDeColumnas(KEY);
  expect(leerPreferenciasDeColumnas(KEY)).toBeNull();
});

test("dos claves (dos tenants/usuarios/secciones) no se pisan entre sí", () => {
  guardarPreferenciasDeColumnas("tenant-1:user-1:products", {
    visible: ["name"],
    known: ["name"],
  });
  guardarPreferenciasDeColumnas("tenant-2:user-2:products", {
    visible: ["sku"],
    known: ["sku"],
  });
  expect(leerPreferenciasDeColumnas("tenant-1:user-1:products")?.visible).toEqual(["name"]);
  expect(leerPreferenciasDeColumnas("tenant-2:user-2:products")?.visible).toEqual(["sku"]);
});
