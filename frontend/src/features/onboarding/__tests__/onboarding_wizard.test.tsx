import "@testing-library/jest-dom";
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { OnboardingWizard } from "../OnboardingWizard";
import { onboardingService } from "@/services/onboarding.service";
import { ingestionService } from "@/services/ingestion.service";

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
const mockUpload = ingestionService.upload as jest.MockedFunction<
  typeof ingestionService.upload
>;

/** Completa el paso "Datos principales" y avanza. */
async function completarDatos(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText(/Cuánto vendés por semana/i), "100000");
  await user.type(screen.getByLabelText(/productos distintos/i), "20");
  await user.type(screen.getByLabelText(/proveedores/i), "3");
  await user.click(screen.getByRole("button", { name: "Mi caja" }));
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
    mockSubmit.mockResolvedValue({
      snapshot_id: "s1",
      data_completeness_score: 80,
      confidence_level: "HIGH",
      message: "ok",
    });
  });

  test("arranca en 'Datos principales': ya no pregunta el rubro", () => {
    renderWizard();
    // El rubro lo asigna el dueño al aprobar la solicitud.
    expect(screen.queryByText(/Qué tipo de negocio tenés/i)).toBeNull();
    expect(screen.queryByText(/Tipo de negocio/i)).toBeNull();
    expect(screen.getByText(/Contanos sobre tu negocio/i)).toBeInTheDocument();
  });

  test("muestra el aviso de confidencialidad arriba del régimen fiscal", () => {
    renderWizard();
    expect(screen.getByText(/Esta información es confidencial/)).toBeInTheDocument();
    expect(
      screen.getByText(/no reporta a ARCA ni comparte tu información/),
    ).toBeInTheDocument();
    // El aviso del selector fiscal es OTRO y sigue existiendo.
    expect(screen.getByText(/no bloquea ninguna función/i)).toBeInTheDocument();
  });

  test("el aviso NO ofrece el escape de la facturación: acá el campo es obligatorio", () => {
    // El aviso es una constante compartida con el formulario público, donde la
    // banda de facturación sí tiene "prefiero no decirlo". Acá el campo
    // equivalente es `weekly_sales_estimate_ars`, que el backend exige con
    // `Field(gt=0)`: mostrar la frase prometería una salida que este formulario
    // no da, en el único texto cuyo propósito es generar confianza.
    renderWizard();
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
});
