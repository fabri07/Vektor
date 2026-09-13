import { downloadBlob } from "@/lib/csv";

/**
 * Cambio 3: `downloadBlob` es lo nuevo (usado por `ExportAllFieldsButton`
 * para materializar la respuesta de `GET /export/{entity_type}`). jsdom no
 * implementa `URL.createObjectURL`/`revokeObjectURL` — se mockean acá, no en
 * el módulo real.
 */

test("crea un link con el nombre pedido y libera la URL del objeto", () => {
  const createObjectURL = jest.fn().mockReturnValue("blob:mock-url");
  const revokeObjectURL = jest.fn();
  Object.defineProperty(window.URL, "createObjectURL", { value: createObjectURL });
  Object.defineProperty(window.URL, "revokeObjectURL", { value: revokeObjectURL });

  const clickSpy = jest.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

  const blob = new Blob(["a,b\n1,2"], { type: "text/csv" });
  downloadBlob("vektor-product-completo-2026-09-13.csv", blob);

  expect(createObjectURL).toHaveBeenCalledWith(blob);
  expect(clickSpy).toHaveBeenCalledTimes(1);
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");

  clickSpy.mockRestore();
});
