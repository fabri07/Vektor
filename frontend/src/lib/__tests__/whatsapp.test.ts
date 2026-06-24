import { isLikelyValidWhatsApp, whatsappDigits } from "@/lib/fiscal";

describe("whatsappDigits", () => {
  it("prefija 54 cuando falta el código de país", () => {
    expect(whatsappDigits("11 1234-5678")).toBe("541112345678");
  });

  it("no duplica el 54 si ya viene", () => {
    expect(whatsappDigits("+54 9 11 1234-5678")).toBe("5491112345678");
  });

  it("devuelve vacío sin dígitos", () => {
    expect(whatsappDigits("sin numero")).toBe("");
  });
});

describe("isLikelyValidWhatsApp", () => {
  it("acepta un número AR completo", () => {
    expect(isLikelyValidWhatsApp("11 1234-5678")).toBe(true);
  });

  it("rechaza números demasiado cortos", () => {
    expect(isLikelyValidWhatsApp("1234")).toBe(false);
  });

  it("rechaza null/undefined/vacío", () => {
    expect(isLikelyValidWhatsApp(null)).toBe(false);
    expect(isLikelyValidWhatsApp(undefined)).toBe(false);
    expect(isLikelyValidWhatsApp("")).toBe(false);
  });
});

describe("gmail compose encoding", () => {
  // Espeja gmailComposeUrl de ContactCommunication: el destinatario va URL-encoded.
  const gmailComposeUrl = (email: string) =>
    `https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(email)}`;

  it("escapa caracteres especiales en el email", () => {
    expect(gmailComposeUrl("juan+ventas@correo.com")).toContain(
      "to=juan%2Bventas%40correo.com",
    );
  });
});
