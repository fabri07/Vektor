import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { OnboardingWizard } from "../OnboardingWizard";
import { onboardingService } from "@/services/onboarding.service";
import { ingestionService } from "@/services/ingestion.service";
import { MAIN_CONCERN_OPTIONS, labelOf } from "@/lib/accessRequestOptions";

// `Step4Loading` (paso 3) usa `useRouter` para saltar al dashboard.
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn(), replace: jest.fn() }),
}));
jest.mock("@/services/onboarding.service", () => ({
  onboardingService: { submit: jest.fn(), getStatus: jest.fn() },
}));
jest.mock("@/services/ingestion.service", () => ({
  ingestionService: { upload: jest.fn() },
}));

const mockSubmit = onboardingService.submit as jest.MockedFunction<
  typeof onboardingService.submit
>;
const mockStatus = onboardingService.getStatus as jest.MockedFunction<
  typeof onboardingService.getStatus
>;
const mockUpload = ingestionService.upload as jest.MockedFunction<
  typeof ingestionService.upload
>;

/**
 * Status por default: sin `main_concern` sellada, así que el wizard SÍ la
 * pregunta. Es el caso de una solicitud vieja o de un valor no confiable.
 */
function statusSinConcern() {
  mockStatus.mockResolvedValue({
    completed: false,
    vertical_code: "kiosco_almacen",
    data_completeness_score: null,
    main_concern: null,
  });
}

/** Completa el paso "Datos principales" y avanza. */
async function completarDatos(user: ReturnType<typeof userEvent.setup>) {
  await user.type(
    await screen.findByLabelText(/Cuánto vendés por semana/i),
    "100000",
  );
  await user.type(screen.getByLabelText(/productos distintos/i), "20");
  await user.type(screen.getByLabelText(/proveedores/i), "3");
  await user.click(screen.getByRole("button", { name: labelOf(MAIN_CONCERN_OPTIONS, "CASH") }));
  await user.click(screen.getByRole("button", { name: "Siguiente" }));
}

/** El paso 3 (`Step4Loading`) hace polling con TanStack Query. */
function renderWizard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <OnboardingWizard />
    </QueryClientProvider>,
  );
}

describe("OnboardingWizard", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    statusSinConcern();
    mockSubmit.mockResolvedValue({
      snapshot_id: "s1",
      data_completeness_score: 80,
      confidence_level: "HIGH",
      message: "ok",
    });
  });

  test("arranca en 'Datos principales': ya no pregunta el rubro", async () => {
    renderWizard();
    await screen.findByText(/Contanos sobre tu negocio/i);
    // El rubro lo asigna el dueño al aprobar la solicitud.
    expect(screen.queryByText(/Qué tipo de negocio tenés/i)).toBeNull();
    expect(screen.queryByText(/Tipo de negocio/i)).toBeNull();
    expect(screen.getByText(/Contanos sobre tu negocio/i)).toBeInTheDocument();
  });

  test("muestra el aviso de confidencialidad arriba del régimen fiscal", async () => {
    renderWizard();
    await screen.findByText(/Esta información es confidencial/);
    expect(screen.getByText(/Esta información es confidencial/)).toBeInTheDocument();
    expect(
      screen.getByText(/no reporta a ARCA ni comparte tu información/),
    ).toBeInTheDocument();
    // El aviso del selector fiscal es OTRO y sigue existiendo.
    expect(screen.getByText(/no bloquea ninguna función/i)).toBeInTheDocument();
  });

  test("el aviso NO ofrece el escape de la facturación: acá el campo es obligatorio", async () => {
    // El aviso es una constante compartida con el formulario público, donde la
    // banda de facturación sí tiene "prefiero no decirlo". Acá el campo
    // equivalente es `weekly_sales_estimate_ars`, que el backend exige con
    // `Field(gt=0)`: mostrar la frase prometería una salida que este formulario
    // no da, en el único texto cuyo propósito es generar confianza.
    renderWizard();
    await screen.findByLabelText(/Cuánto vendés por semana/i);
    expect(screen.queryByText(/La pregunta de facturación es opcional/)).toBeNull();
    // OJO: acá SÍ hay un "Prefiero no decirlo ahora", pero es del selector de
    // RÉGIMEN FISCAL, que es opcional de verdad. Por eso el assert de arriba
    // mira la frase del aviso y no ese texto suelto, que daría un falso
    // negativo. El campo que la frase prometería poder saltear es este, y no
    // tiene ninguna opción de omitirlo: el backend lo exige con `Field(gt=0)`
    // y `Step2Form.validate()` corta con "Ingresá un monto mayor a 0.".
    expect(screen.getByLabelText(/Cuánto vendés por semana/i)).toBeInTheDocument();
  });

  test("el submit NO manda vertical_code: el schema del backend lo prohíbe", async () => {
    const user = userEvent.setup({ delay: null });
    renderWizard();

    await completarDatos(user);
    // Paso "Archivos": continuar sin adjuntar nada dispara el submit.
    await user.click(screen.getByRole("button", { name: "Continuar" }));

    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    const payload = mockSubmit.mock.calls[0]![0] as unknown as Record<string, unknown>;
    // `OnboardingSubmitRequest` usa extra="forbid" y ya no declara este campo:
    // mandarlo es un 422 que deja al tenant sin onboarding ni score.
    expect(payload).not.toHaveProperty("vertical_code");
    expect(payload).toMatchObject({
      weekly_sales_estimate_ars: 100000,
      main_concern: "CASH",
    });
    expect(mockUpload).not.toHaveBeenCalled();
  });

  /**
   * Dejar un monto en blanco NO es contestar cero.
   *
   * `validate()` armaba el payload con `parseFloat(campo) || 0`: los tres
   * campos de plata son opcionales en la UI, así que saltearlos mandaba un
   * cero afirmado que el backend persistía como estimación del dueño y el
   * score usaba para calcular. El usuario no tenía forma de enterarse de que
   * saltear la pregunta equivalía a decir "no gasto nada".
   *
   * Los dos tests van juntos: uno prueba la ausencia, el otro el cero
   * explícito. Solos no distinguen el fix del bug.
   */
  test("los montos en blanco viajan como null, no como 0", async () => {
    const user = userEvent.setup({ delay: null });
    renderWizard();

    await completarDatos(user); // no toca ninguno de los tres montos
    await user.click(screen.getByRole("button", { name: "Continuar" }));

    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    const payload = mockSubmit.mock.calls[0]![0];
    expect(payload.monthly_inventory_cost_ars).toBeNull();
    expect(payload.monthly_fixed_expenses_ars).toBeNull();
    expect(payload.cash_on_hand_ars).toBeNull();
  });

  test("un cero tipeado a propósito sí viaja como 0", async () => {
    const user = userEvent.setup({ delay: null });
    renderWizard();

    await user.type(
      await screen.findByLabelText(/Cuánto vendés por semana/i),
      "100000",
    );
    await user.type(screen.getByLabelText(/gastás en mercadería/i), "0");
    await user.type(screen.getByLabelText(/productos distintos/i), "20");
    await user.type(screen.getByLabelText(/proveedores/i), "3");
    await user.click(screen.getByRole("button", { name: labelOf(MAIN_CONCERN_OPTIONS, "CASH") }));
    await user.click(screen.getByRole("button", { name: "Siguiente" }));
    await user.click(screen.getByRole("button", { name: "Continuar" }));

    await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
    const payload = mockSubmit.mock.calls[0]![0];
    expect(payload.monthly_inventory_cost_ars).toBe(0);
    // Y los que sí quedaron en blanco siguen siendo null.
    expect(payload.cash_on_hand_ars).toBeNull();
  });

  /**
   * La preocupación principal se pregunta en el formulario público de
   * solicitud, y la aprobación la sella en el perfil. El backend ya la tomaba
   * de ahí cuando el body no la traía — pero el wizard la preguntaba igual y
   * la mandaba siempre, así que ese fallback era código muerto y la respuesta
   * del onboarding pisaba la del screening. Preguntar dos veces lo mismo y
   * quedarse con la segunda respuesta borra la primera sin que nadie se entere.
   */
  describe("preocupación principal ya declarada al pedir acceso", () => {
    function statusConConcern() {
      mockStatus.mockResolvedValue({
        completed: false,
        vertical_code: "kiosco_almacen",
        data_completeness_score: null,
        main_concern: "STOCK",
      });
    }

    test("no se vuelve a preguntar, y el payload la omite", async () => {
      statusConConcern();
      const user = userEvent.setup({ delay: null });
      renderWizard();

      await screen.findByLabelText(/Cuánto vendés por semana/i);
      expect(screen.queryByText(/Qué te preocupa más hoy/i)).toBeNull();

      await user.type(screen.getByLabelText(/Cuánto vendés por semana/i), "100000");
      await user.type(screen.getByLabelText(/productos distintos/i), "20");
      await user.type(screen.getByLabelText(/proveedores/i), "3");
      // Sin la pregunta, "Siguiente" ya no queda bloqueado por ella.
      await user.click(screen.getByRole("button", { name: "Siguiente" }));
      await user.click(screen.getByRole("button", { name: "Continuar" }));

      await waitFor(() => expect(mockSubmit).toHaveBeenCalledTimes(1));
      // Se omite a propósito: el backend la resuelve desde el perfil. Mandarla
      // sería un viaje de ida y vuelta de un dato que ya tiene.
      expect(mockSubmit.mock.calls[0]![0]).not.toHaveProperty("main_concern");
    });

    test("si el status no la trae, se pregunta igual (fail-safe)", async () => {
      statusSinConcern();
      renderWizard();
      expect(await screen.findByText(/Qué te preocupa más hoy/i)).toBeInTheDocument();
    });

    test("si el status falla, se pregunta igual", async () => {
      mockStatus.mockRejectedValue(new Error("500"));
      renderWizard();
      // Nunca al revés: saltear la pregunta con un valor que no llegó dejaría
      // el alta sin `main_concern` y sin forma de cargarla después.
      expect(await screen.findByText(/Qué te preocupa más hoy/i)).toBeInTheDocument();
    });

    test("las dos pantallas usan el MISMO texto para las opciones", async () => {
      statusSinConcern();
      renderWizard();
      // Antes había dos catálogos escritos a mano sobre los mismos tres
      // valores: "Mis márgenes / Mi stock / Mi caja" acá y otro distinto en la
      // solicitud. El visitante que ya contestó no reconocía la segunda como la
      // misma pregunta.
      //
      // Se compara contra MAIN_CONCERN_OPTIONS, no contra literales: la
      // invariante es "ambas pantallas rinden el MISMO catálogo", no "el
      // catálogo dice tal cosa". Con literales, cada copy pass volvería a
      // romper este test sin que la invariante se haya movido — que es
      // exactamente lo que pasó en 2026-08-18. Los rótulos visibles los fija el
      // test de contrato en lib/__tests__/access_request_options.test.ts.
      await screen.findByRole("button", { name: labelOf(MAIN_CONCERN_OPTIONS, "CASH") });
      for (const { label } of MAIN_CONCERN_OPTIONS) {
        expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
      }
    });
  });

  test("el copy avisa que dejar en blanco no es poner cero", async () => {
    renderWizard();
    await screen.findByLabelText(/Cuánto vendés por semana/i);
    expect(
      screen.getAllByText(/no es lo mismo que poner 0/i).length,
    ).toBeGreaterThanOrEqual(3);
  });
});
