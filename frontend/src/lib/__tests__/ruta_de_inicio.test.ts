import { rutaDeInicio, rutaDelSoporte } from "@/lib/roles";

describe("rutaDeInicio", () => {
  it("el soporte aterriza en su consola; el resto en el dashboard", () => {
    expect(rutaDeInicio("SUPERADMIN")).toBe("/admin");
    expect(rutaDeInicio("OWNER")).toBe("/dashboard");
    expect(rutaDeInicio("ADMIN")).toBe("/dashboard");
    expect(rutaDeInicio("CASHIER")).toBe("/dashboard");
    expect(rutaDeInicio(undefined)).toBe("/dashboard");
  });
});

describe("rutaDelSoporte", () => {
  it("sólo /admin y la configuración de su cuenta", () => {
    for (const ok of ["/admin", "/admin/tenants", "/admin/tenants/abc", "/settings"]) {
      expect(rutaDelSoporte(ok)).toBe(true);
    }
  });

  it("las pantallas de negocio no, ni las que sólo empiezan igual", () => {
    for (const no of ["/dashboard", "/chat", "/sales", "/products", "/pos", "/administrador", "/settingsx"]) {
      expect(rutaDelSoporte(no)).toBe(false);
    }
  });
});
