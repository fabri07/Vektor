interface ProgressBarProps {
  currentStep: number;
  totalSteps: number;
}

const STEP_LABELS = ["Tipo de negocio", "Tu negocio", "Archivos", "Analizando"];

export function ProgressBar({ currentStep, totalSteps }: ProgressBarProps) {
  const pct = Math.round((currentStep / totalSteps) * 100);

  return (
    <div className="mb-6 w-full">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-medium text-gray-700">
          Paso {currentStep} de {totalSteps} —{" "}
          <span className="text-gray-500">
            {STEP_LABELS[currentStep - 1] ?? ""}
          </span>
        </span>
        <span className="text-sm text-gray-400">{pct}%</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-gray-200">
        <div
          className="h-1.5 rounded-full bg-[#E63946] transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
