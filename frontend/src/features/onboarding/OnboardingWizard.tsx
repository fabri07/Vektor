"use client";

/**
 * Wizard de onboarding post-login — TRES pasos.
 *
 * El paso de "elegí tu rubro" se eliminó: desde que el acceso es por solicitud
 * aprobada, el vertical lo asigna el dueño al aprobar (`assigned_vertical`), y
 * el payload de `POST /onboarding/submit` **ya no acepta `vertical_code`** — el
 * schema del backend usa `extra="forbid"`, así que mandarlo es un 422.
 */

import { useState } from "react";
import { ProgressBar } from "./ProgressBar";
import { Step2Form, type Step2Data } from "./Step2Form";
import { Step3Upload } from "./Step3Upload";
import { Step4Loading } from "./Step4Loading";
import { onboardingService } from "@/services/onboarding.service";
import { ingestionService } from "@/services/ingestion.service";

/** 1 = datos principales · 2 = archivos · 3 = tu score. */
type Step = 1 | 2 | 3;

interface WizardState {
  step: Step;
  formData: Step2Data | null;
  submitError: string | null;
  isSubmitting: boolean;
  formSubmitted: boolean;
  pendingFile: File | null;
}


export function OnboardingWizard() {
  const [state, setState] = useState<WizardState>({
    step: 1,
    formData: null,
    submitError: null,
    isSubmitting: false,
    formSubmitted: false,
    pendingFile: null,
  });

  function goToStep(step: Step) {
    setState((prev) => ({ ...prev, step, submitError: null }));
  }

  function handleFormSubmit(data: Step2Data) {
    setState((prev) => ({ ...prev, formData: data, step: 2, submitError: null }));
  }

  async function handleUploadNext(file: File | null) {
    if (!state.formData) return;

    setState((prev) => ({ ...prev, isSubmitting: true, submitError: null }));

    if (!state.formSubmitted) {
      try {
        await onboardingService.submit({
          weekly_sales_estimate_ars: state.formData.weekly_sales_estimate_ars,
          monthly_inventory_cost_ars: state.formData.monthly_inventory_cost_ars,
          monthly_fixed_expenses_ars: state.formData.monthly_fixed_expenses_ars,
          cash_on_hand_ars: state.formData.cash_on_hand_ars,
          product_count_estimate: state.formData.product_count_estimate,
          supplier_count_estimate: state.formData.supplier_count_estimate,
          main_concern: state.formData.main_concern,
          work_days: state.formData.work_days,
          work_open_hour: state.formData.work_open_hour,
          work_close_hour: state.formData.work_close_hour,
          ...(state.formData.fiscal_condition
            ? { fiscal_condition: state.formData.fiscal_condition }
            : {}),
        });
        setState((prev) => ({ ...prev, formSubmitted: true }));
      } catch {
        setState((prev) => ({
          ...prev,
          isSubmitting: false,
          submitError: "Hubo un problema al enviar los datos. Intentá de nuevo.",
        }));
        return;
      }
    }

    if (file) {
      try {
        await ingestionService.upload(file);
      } catch {
        setState((prev) => ({
          ...prev,
          isSubmitting: false,
          pendingFile: file,
          submitError:
            "Los datos se guardaron correctamente. El archivo no pudo subirse.",
        }));
        return;
      }
    }

    setState((prev) => ({ ...prev, isSubmitting: false, step: 3 }));
  }

  async function handleRetryUpload() {
    if (!state.pendingFile) return;
    setState((prev) => ({ ...prev, isSubmitting: true, submitError: null }));
    try {
      await ingestionService.upload(state.pendingFile);
      setState((prev) => ({ ...prev, isSubmitting: false, pendingFile: null, step: 3 }));
    } catch {
      setState((prev) => ({
        ...prev,
        isSubmitting: false,
        pendingFile: null,
        submitError: "El archivo sigue sin poder subirse.",
      }));
    }
  }

  const { step, formData, submitError, isSubmitting, pendingFile, formSubmitted } = state;

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-vk-bg-light">
      <div className="flex min-h-full items-start justify-center px-4 py-10 sm:items-center sm:py-16">
        <div className="w-full max-w-2xl">
          {/* Progress bar — visible en todos los pasos; el 3 es "Tu score" */}
          <ProgressBar currentStep={step} />

          {/* Card */}
          <div className="rounded-2xl border border-gray-200 bg-white px-4 py-6 shadow-sm sm:px-8 sm:py-8 md:px-10">
            {/* El formulario es ahora el primer paso: no hay "Anterior". */}
            {step === 1 && (
              <Step2Form initialData={formData} onSubmit={handleFormSubmit} />
            )}

            {step === 2 && (
              <>
                <Step3Upload onNext={handleUploadNext} />

                {submitError && (
                  <div className="mt-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
                    <p>{submitError}</p>
                    {pendingFile ? (
                      <div className="mt-3">
                        <button
                          type="button"
                          onClick={handleRetryUpload}
                          disabled={isSubmitting}
                          className="rounded-lg bg-vk-navy px-4 py-2 text-xs font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-40"
                        >
                          Reintentar archivo
                        </button>
                      </div>
                    ) : formSubmitted ? (
                      <button
                        type="button"
                        onClick={() => goToStep(3)}
                        className="mt-3 rounded-lg border border-red-300 px-4 py-2 text-xs font-semibold text-red-600 transition-colors hover:bg-red-100"
                      >
                        Continuar sin archivo
                      </button>
                    ) : null}
                  </div>
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
                    onClick={() => goToStep(1)}
                    className="text-sm text-vk-text-muted underline underline-offset-2 hover:text-vk-text-primary transition-colors"
                  >
                    Anterior
                  </button>
                </div>
              </>
            )}

            {step === 3 && <Step4Loading />}
          </div>

          {/* Footer note */}
          {step < 3 && (
            <p className="mt-4 text-center text-xs text-vk-text-muted">
              Tu información es privada y solo se usa para calcular tu salud financiera.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
