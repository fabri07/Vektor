import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AccessRequestForm } from "../AccessRequestForm";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({ api: { post: jest.fn() } }));

const mockPush = jest.fn();
let searchParams = new URLSearchParams();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush, replace: jest.fn() }),
  useSearchParams: () => searchParams,
}));

const mockPost = api.post as jest.MockedFunction<typeof api.post>;

function renderForm() {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <AccessRequestForm />
    </QueryClientProvider>,
  );
}

type User = ReturnType<typeof userEvent.setup>;

/** Completa el bloque de contacto. */
async function fillContacto(user: User) {
  await user.type(screen.getByLabelText(/Nombre y apellido/i), "Ana Pérez");
  await user.type(screen.getByLabelText(/^Email/i), "ana@negocio.com");
  await user.type(screen.getByLabelText(/Nombre del negocio/i), "Kiosco Ana");
}

/** Los 8 grupos de opción cerrada, con una etiqueta unívoca de cada uno. */
const GRUPOS: ReadonlyArray<readonly [string, RegExp]> = [
  ["years_operating", /Más de 5 años/i],
  ["staff_size", /Solo yo/i],
  ["main_concern", /El margen/i],
  ["monthly_revenue_band", /Prefiero no decirlo/i],
  ["records_format", /Excel o Google Sheets/i],
  ["history_depth", /Entre 1 y 3 años/i],
  ["can_share_files", /Sí, y están ordenados/i],
  ["requested_plan", /Cuenta gratuita/i],
];

/** Completa el screening (tu negocio + tu info) y el plan. */
async function fillScreening(user: User, omitir: readonly string[] = []) {
  for (const [campo, etiqueta] of GRUPOS) {
    if (omitir.includes(campo)) continue;
    await user.click(screen.getByLabelText(etiqueta));
  }
}

/** Claves exactas del cuerpo de `POST /access-requests`. */
const CLAVES_DEL_PAYLOAD = [
  "full_name",
  "email",
  "phone",
  "business_name",
  "requested_vertical",
  "vertical_other_text",
  "requested_plan",
  "years_operating",
  "staff_size",
  "monthly_revenue_band",
  "main_concern",
  "records_format",
  "history_depth",
  "can_share_files",
  "records_notes",
  "applicant_notes",
  "consent",
  "consent_version",
  "cta_source",
  "website",
  "elapsed_ms",
];

function submitButton() {
  return screen.getByRole("button", { name: /Pedir acceso/i });
}

/**
 * Timeout de los tests que completan el formulario entero.
 *
 * Este formulario tiene 12 campos requeridos, así que llenarlo encadena una
 * docena larga de `user.click`/`user.type`, y cada uno de esos gestos arrastra
 * su propio ciclo de `act()` + timers. Con los 5000 ms que Jest da por defecto
 * entran holgados corriendo solos, pero no cuando los workers de jest compiten
 * por CPU (el CI tiene menos cores y más lentos que una máquina de desarrollo).
 *
 * **No es lentitud del componente ni una carrera**: el mismo suite con
 * `--runInBand` pasa entero y sin timeouts. Es el costo de simular a un humano
 * completando un formulario largo, y va acotado a los tests que lo hacen — la
 * config global de Jest sigue en 5000 ms para que un test genuinamente lento en
 * cualquier otra parte del repo se siga notando.
 */
const TIMEOUT_FORMULARIO_COMPLETO = 20_000;

describe("AccessRequestForm", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    searchParams = new URLSearchParams();
  });

  test("renderiza las secciones del formulario y el aviso de confidencialidad", () => {
    renderForm();
    expect(screen.getByText("Contacto")).toBeInTheDocument();
    expect(screen.getByText("Rubro")).toBeInTheDocument();
    expect(screen.getByText("Tu negocio")).toBeInTheDocument();
    expect(screen.getByText("Tu info")).toBeInTheDocument();
    expect(screen.getByText("Cómo querés usar Véktor")).toBeInTheDocument();
    expect(screen.getByText(/Esta información es confidencial/)).toBeInTheDocument();
    expect(
      screen.getByText(/no reporta a ARCA ni comparte tu información/),
    ).toBeInTheDocument();
  });

  test("el aviso SÍ ofrece el escape de la facturación: acá existe", () => {
    // Su contraparte vive en `onboarding_wizard.test.tsx`: el mismo aviso, sin
    // esta frase, porque allá el campo de facturación es obligatorio (`gt=0`) y
    // no hay "prefiero no decirlo". Los dos tests juntos son la red; uno solo
    // pasaría en verde con el texto compartido y la promesa incumplida.
    renderForm();
    expect(
      screen.getByText(/La pregunta de facturación es opcional/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/prefiero no decirlo/i)).toBeInTheDocument();
  });

  test("NO existe ningún campo de contraseña: este formulario no crea la cuenta", () => {
    const { container } = renderForm();
    expect(container.querySelectorAll('input[type="password"]')).toHaveLength(0);
  });

  test("al montar NO hay ningún mensaje de error visible", () => {
    renderForm();
    // El borrador vacío es inválido, pero el usuario todavía no tocó nada: la
    // puerta de entrada pública no puede abrir con campos en rojo.
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
    expect(screen.queryByText(/Escribí tu nombre/i)).toBeNull();
    expect(screen.queryByText(/Email inválido/i)).toBeNull();
    expect(screen.queryByText(/Escribí el nombre de tu negocio/i)).toBeNull();
    // Tampoco el resumen de faltantes: no hubo intento de envío.
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("el resumen de faltantes NO se abre con el primer blur", async () => {
    const user = userEvent.setup({ delay: null });
    renderForm();

    await user.type(screen.getByLabelText(/Nombre y apellido/i), "Ana Pérez");
    await user.tab();

    // El campo propio ya puede hablar, pero el panel "Te faltan N respuestas"
    // no tiene por qué abrirse cuando recién vas por el campo 1.
    expect(screen.queryByRole("status")).toBeNull();
  });

  test("un campo de texto solo muestra su error después de tocarlo", async () => {
    const user = userEvent.setup({ delay: null });
    renderForm();

    const nombre = screen.getByLabelText(/Nombre y apellido/i);
    await user.type(nombre, "A"); // corto, pero todavía sin salir del campo
    expect(screen.queryByText(/Escribí tu nombre/i)).toBeNull();

    await user.tab(); // blur → recién ahora hablamos
    expect(await screen.findByText(/Escribí tu nombre/i)).toBeInTheDocument();
  });

  test("un grupo de opción sin elegir muestra su error al intentar enviar", async () => {
    const user = userEvent.setup({ delay: null });
    const { container } = renderForm();

    await fillContacto(user);
    await user.click(screen.getByRole("button", { name: /Kiosco/i }));
    await fillScreening(user, ["staff_size"]);
    await user.click(screen.getByRole("checkbox"));

    // Mientras no se intente enviar, el grupo sin tocar sigue callado.
    expect(screen.queryByText("Elegí una opción")).toBeNull();

    // El botón está gris, así que el intento llega por envío implícito.
    fireEvent.submit(container.querySelector("form")!);

    expect(await screen.findByText("Elegí una opción")).toBeInTheDocument();
    // El foco va al primer control del campo faltante: es la mitad que vuelve
    // útil al panel (jest.setup.ts ya mockea scrollIntoView).
    expect(document.activeElement).toBe(
      document.getElementById("campo-staff_size"),
    );
    // Y el resumen nombra exactamente el campo que falta.
    const resumen = screen.getByRole("status");
    expect(resumen).toHaveTextContent("Te falta responder una cosa:");
    expect(resumen).toHaveTextContent("¿Cuánta gente trabaja?");
    expect(mockPost).not.toHaveBeenCalled();
  }, TIMEOUT_FORMULARIO_COMPLETO);

  /**
   * Accesibilidad de los campos de texto.
   *
   * El defecto que esto protege es sutil: envolviendo label + hint + control +
   * error en un mismo `<label>`, la asociación es implícita y TODO el contenido
   * del label entra en el nombre accesible. El input terminaba llamándose
   * "Nombre y apellido * Escribí tu nombre y apellido" —el error leído como
   * parte de la etiqueta— y ningún campo se anunciaba como inválido. Por eso
   * los tests miran el NOMBRE ACCESIBLE resultante, no solo que los atributos
   * existan: `aria-invalid` puesto sobre un label contaminado no arregla nada.
   */
  describe("accesibilidad de los campos", () => {
    test("el error no se cuela en el nombre accesible del campo", async () => {
      const user = userEvent.setup({ delay: null });
      renderForm();

      // Antes del error: el nombre es la etiqueta, y nada más.
      expect(
        screen.getByRole("textbox", { name: "Nombre y apellido *" }),
      ).toBeInTheDocument();

      await user.type(screen.getByLabelText(/Nombre y apellido/i), "A");
      await user.tab();
      await screen.findByText(/Escribí tu nombre/i);

      // Y después del error: sigue siendo la etiqueta.
      const nombre = screen.getByRole("textbox", { name: "Nombre y apellido *" });
      expect(nombre).toHaveAccessibleName("Nombre y apellido *");
      expect(nombre.getAttribute("aria-label")).toBeNull();
    });

    test("un campo con error se anuncia inválido y describe el error", async () => {
      const user = userEvent.setup({ delay: null });
      renderForm();

      const nombre = screen.getByLabelText(/Nombre y apellido/i);
      // Mudo mientras nadie lo tocó: no se anuncia inválido de arranque.
      expect(nombre).not.toHaveAttribute("aria-invalid");
      expect(nombre).not.toHaveAttribute("aria-describedby");

      await user.type(nombre, "A");
      await user.tab();
      await screen.findByText(/Escribí tu nombre/i);

      expect(nombre).toHaveAttribute("aria-invalid", "true");
      expect(nombre).toHaveAccessibleDescription("Escribí tu nombre y apellido");
    });

    test("el hint describe el campo sin meterse en su nombre", () => {
      renderForm();
      const telefono = screen.getByRole("textbox", {
        name: "Teléfono / WhatsApp (opcional)",
      });
      expect(telefono).toHaveAccessibleDescription("Si nos lo dejás, te escribimos por acá.");
    });
  });

  test("el honeypot está presente, oculto, y con un name que el autofill no conoce", () => {
    const { container } = renderForm();
    const honeypot = container.querySelector<HTMLInputElement>('input[name="empresa_url"]');
    expect(honeypot).not.toBeNull();
    expect(honeypot).toHaveClass("hidden");
    expect(honeypot).toHaveAttribute("tabindex", "-1");
    /*
     * Lo que este test protege de verdad: NINGÚN input del formulario se llama
     * `website`. Ese nombre lo completa solo el autofill de Chrome y los
     * gestores de contraseñas, y un honeypot lleno hace que el backend
     * descarte la solicitud sin persistir nada y devolviendo el mismo 201
     * genérico — el visitante ve "te mandamos un link" y nunca llega nada.
     */
    expect(container.querySelector('input[name="website"]')).toBeNull();
    expect(honeypot).toHaveAttribute("autocomplete", "new-password");
  });

  test("elegir 'Otro' revela el textarea y sin texto el submit sigue bloqueado", async () => {
    const user = userEvent.setup({ delay: null });
    renderForm();

    expect(screen.queryByLabelText(/Contanos de qué es tu negocio/i)).toBeNull();

    await fillContacto(user);
    await fillScreening(user);
    await user.click(screen.getByRole("button", { name: /Otro/i }));
    await user.click(screen.getByRole("checkbox"));

    const textarea = screen.getByLabelText(/Contanos de qué es tu negocio/i);
    expect(textarea).toBeInTheDocument();
    // "Otros" sin descripción: el backend lo rechazaría (validación no-vacío).
    expect(submitButton()).toBeDisabled();

    // Espacios en blanco tampoco alcanzan: se recortan antes de medir.
    await user.type(textarea, "   ");
    expect(submitButton()).toBeDisabled();

    await user.type(textarea, "Ferretería de barrio");
    await waitFor(() => expect(submitButton()).toBeEnabled());
  }, TIMEOUT_FORMULARIO_COMPLETO);

  test("el submit está bloqueado hasta que el formulario es válido", async () => {
    const user = userEvent.setup({ delay: null });
    renderForm();

    expect(submitButton()).toBeDisabled();

    await fillContacto(user);
    expect(submitButton()).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /Kiosco/i }));
    expect(submitButton()).toBeDisabled();

    await fillScreening(user);
    // Falta el consentimiento: es obligatorio, no decorativo.
    expect(submitButton()).toBeDisabled();

    await user.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(submitButton()).toBeEnabled());
  }, TIMEOUT_FORMULARIO_COMPLETO);

  test("envío exitoso: postea el contrato exacto (con consent, sin password)", async () => {
    mockPost.mockResolvedValueOnce({ data: { status: "ok", message: "ok" } });
    const user = userEvent.setup({ delay: null });
    renderForm();

    await fillContacto(user);
    await user.click(screen.getByRole("button", { name: /Kiosco/i }));
    await fillScreening(user);
    await user.click(screen.getByRole("checkbox"));
    await user.click(submitButton());

    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    const [url, payload] = mockPost.mock.calls[0]!;
    expect(url).toBe("/access-requests");

    const cuerpo = payload as Record<string, unknown>;
    expect(cuerpo).toMatchObject({
      full_name: "Ana Pérez",
      email: "ana@negocio.com",
      business_name: "Kiosco Ana",
      requested_vertical: "kiosco_almacen",
      requested_plan: "free",
      years_operating: "gt_5y",
      staff_size: "solo",
      main_concern: "MARGIN",
      monthly_revenue_band: "no_contesta",
      records_format: "planilla",
      history_depth: "1y_3y",
      can_share_files: "si_ordenados",
      consent: true,
      website: "",
    });
    // Sin rubro "otros" el texto libre viaja null: mandarlo sería 422.
    expect(cuerpo.vertical_other_text).toBeNull();
    expect(typeof cuerpo.elapsed_ms).toBe("number");
    // El endpoint usa extra="forbid": un campo de MÁS es 422. `toMatchObject`
    // no detecta claves de sobra, así que se comparan exactamente.
    expect(Object.keys(cuerpo).sort()).toEqual([...CLAVES_DEL_PAYLOAD].sort());
    expect(cuerpo).not.toHaveProperty("password");

    await waitFor(() =>
      expect(mockPush).toHaveBeenCalledWith(
        "/solicitud-enviada?email=ana%40negocio.com",
      ),
    );
  }, TIMEOUT_FORMULARIO_COMPLETO);

  test("?plan=premium precarga el plan y lo deja editable", async () => {
    searchParams = new URLSearchParams("plan=premium");
    const user = userEvent.setup({ delay: null });
    renderForm();

    await waitFor(() =>
      expect(screen.getByLabelText(/Cuenta Premium/i)).toBeChecked(),
    );

    await user.click(screen.getByLabelText(/Cuenta gratuita/i));
    expect(screen.getByLabelText(/Cuenta gratuita/i)).toBeChecked();
    expect(screen.getByLabelText(/Cuenta Premium/i)).not.toBeChecked();
  });

  test("un error del backend muestra el mensaje y no navega", async () => {
    mockPost.mockRejectedValueOnce(new Error("network"));
    const user = userEvent.setup({ delay: null });
    renderForm();

    await fillContacto(user);
    await user.click(screen.getByRole("button", { name: /Kiosco/i }));
    await fillScreening(user);
    await user.click(screen.getByRole("checkbox"));
    await user.click(submitButton());

    expect(await screen.findByText(/No pudimos enviar tu solicitud/i)).toBeInTheDocument();
    expect(mockPush).not.toHaveBeenCalled();
  }, TIMEOUT_FORMULARIO_COMPLETO);
});
