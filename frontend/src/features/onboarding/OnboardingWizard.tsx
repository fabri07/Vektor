"use client";

import { useState } from "react";
import { ProgressBar } from "./ProgressBar";
import { Step1Vertical, type Vertical } from "./Step1Vertical";
import { Step2Form, type Step2Data } from "./Step2Form";
import { Step3Upload } from "./Step3Upload";
import { Step4Loading } from "./Step4Loading";
import { onboardingService } from "@/services/onboarding.service";
import { ingestionService } from "@/services/ingestion.service";

type Step = 1 | 2 | 3 | 4;

interface WizardState {
  step: Step;
  vertical: Vertical | null;
  formData: Step2Data | null;
  submitError: string | null;
  isSubmitting: boolean;
}


export function OnboardingWizard() {
  const [state, setState] = useState<WizardState>({
    step: 1,
    vertical: null,
    formData: null,
    submitError: null,
    isSubmitting: false,
  });

  function goToStep(step: Step) {
    setState((prev) => ({ ...prev, step, submitError: null }));
  }

  function handleStep1Next() {
    if (!state.vertical) return;
    goToStep(2);
  }

  function handleStep2Submit(data: Step2Data) {
    setState((prev) => ({ ...prev, formData: data, step: 3, submitError: null }));
  }

  async function handleStep3Next(file: File | null) {
    if (!state.vertical || !state.formData) return;

    setState((prev) => ({ ...prev, isSubmitting: true, submitError: null }));

    try {
      await onboardingService.submit({
        vertical_code: state.vertical,
        weekly_sales_estimate_ars: state.formData.weekly_sales_estimate_ars,
        monthly_inventory_cost_ars: state.formData.monthly_inventory_cost_ars,
        monthly_fixed_expenses_ars: state.formData.monthly_fixed_expenses_ars,
        cash_on_hand_ars: state.formData.cash_on_hand_ars,
        product_count_estimate: state.formData.product_count_estimate,
        supplier_count_estimate: state.formData.supplier_count_estimate,
        main_concern: state.formData.main_concern,
      });

      if (file) {
        try {
          await ingestionService.upload(file);
        } catch {
          setState((prev) => ({
            ...prev,
            isSubmitting: false,
            submitError: "El archivo no pudo subirse. Intentá de nuevo.",
          }));
          return;
        }
      }

      setState((prev) => ({ ...prev, isSubmitting: false, step: 4 }));
    } catch {
      setState((prev) => ({
        ...prev,
        isSubmitting: false,
        submitError:
          "Hubo un problema al enviar los datos. Intentá de nuevo.",
      }));
    }
  }

  const { step, vertical, formData, submitError, isSubmitting } = state;

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-vk-bg-light">
      <div className="flex min-h-full items-start justify-center px-4 py-10 sm:items-center sm:py-16">
        <div className="w-full max-w-2xl">
          {/* Progress bar — visible en todos los pasos, step 4 muestra "Tu score" como actual */}
          <ProgressBar currentStep={step} />

          {/* Card */}
          <div className="rounded-2xl border border-gray-200 bg-white px-4 py-6 shadow-sm sm:px-8 sm:py-8 md:px-10">
            {step === 1 && (
              <>
                <Step1Vertical
                  selected={vertical}
                  onSelect={(v) =>
                    setState((prev) => ({ ...prev, vertical: v }))
                  }
                />
                <div className="mt-8 flex justify-end">
                  <button
                    type="button"
                    disabled={!vertical}
                    onClick={handleStep1Next}
                    className="h-11 rounded-xl bg-vk-navy px-8 text-sm font-semibold text-white transition-opacity hover:opacity-90 focus:outline-none focus:ring-2 focus:ring-vk-navy/30 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    Siguiente
                  </button>
                </div>
              </>
            )}

            {step === 2 && (
              <>
                <Step2Form
                  initialData={formData}
                  onSubmit={handleStep2Submit}
                />
                <div className="mt-4 flex justify-start">
                  <button
                    type="button"
                    onClick={() => goToStep(1)}
                    className="text-sm text-gray-400 underline underline-offset-2 hover:text-gray-600 transition-colors"
                  >
                    Anterior
                  </button>
                </div>
              </>
            )}

            {step === 3 && (
              <>
                <Step3Upload onNext={handleStep3Next} />

                {submitError && (
                  <p className="mt-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
                    {submitError}
                  </p>
                )}

                {isSubmitting && (
                  <div className="mt-4 flex items-center gap-2 text-sm text-gray-500">
                    <div className="h-4 w-4 animate-spin rounded-full border-2 border-gray-300 border-t-gray-600" />
                    Guardando datos...
                  </div>
                )}

                <div className="mt-4 flex justify-start">
                  <button
                    type="button"
                    onClick={() => goToStep(2)}
                    className="text-sm text-gray-400 underline underline-offset-2 hover:text-gray-600 transition-colors"
                  >
                    Anterior
                  </button>
                </div>
              </>
            )}

            {step === 4 && <Step4Loading />}
          </div>

          {/* Footer note */}
          {step < 4 && (
            <p className="mt-4 text-center text-xs text-gray-400">
              Tu información es privada y solo se usa para calcular tu salud financiera.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
