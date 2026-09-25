import { createBrowserPrinter } from "@/lib/print/browserPrinter";

const doc = { title: "T", bodyHtml: "<p>hola</p>", css: "p{color:#000}" };
const iframes = () => document.querySelectorAll("iframe").length;

afterEach(() => {
  document.body.innerHTML = "";
});

describe("browserPrinter", () => {
  it("si print() tira, quita el iframe igual y propaga el error", async () => {
    const printer = createBrowserPrinter({
      printWindow: () => {
        throw new Error("sin impresora");
      },
    });
    await expect(printer.print(doc)).rejects.toThrow("sin impresora");
    expect(iframes()).toBe(0);
  });

  it("escribe el documento, imprime y quita el iframe al afterprint", async () => {
    let contenido = "";
    let ventana: Window | null = null;
    const printer = createBrowserPrinter({
      printWindow: (w) => {
        contenido = w.document.body.innerHTML;
        ventana = w;
      },
    });
    await printer.print(doc);
    expect(contenido).toBe("<p>hola</p>");
    expect(iframes()).toBe(1); // todavía imprimiendo
    (ventana as unknown as Window).dispatchEvent(new Event("afterprint"));
    expect(iframes()).toBe(0);
  });

  it("si el navegador nunca avisa afterprint, lo quita por tiempo", async () => {
    const printer = createBrowserPrinter({ printWindow: () => {}, fallbackRemoveMs: 20 });
    await printer.print(doc);
    expect(iframes()).toBe(1);
    await new Promise((r) => setTimeout(r, 40));
    expect(iframes()).toBe(0);
  });
});
