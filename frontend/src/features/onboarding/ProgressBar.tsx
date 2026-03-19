const MILESTONES = [
  { step: 1, label: "Tipo de negocio" },
  { step: 2, label: "Datos principales" },
  { step: 3, label: "Archivos" },
  { step: 4, label: "Tu score" },
] as const;

interface ProgressBarProps {
  currentStep: number;
}

function CheckIcon() {
  return (
    <svg
      className="h-3.5 w-3.5"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.5}
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
    </svg>
  );
}

export function ProgressBar({ currentStep }: ProgressBarProps) {
  return (
    <nav aria-label="Progreso del onboarding" className="mb-8 w-full">
      <ol className="flex items-center">
        {MILESTONES.map((milestone, idx) => {
          const isCompleted = milestone.step < currentStep;
          const isCurrent = milestone.step === currentStep;
          const isPending = milestone.step > currentStep;
          const isLast = idx === MILESTONES.length - 1;

          return (
            <li
              key={milestone.step}
              className={["flex items-center", !isLast && "flex-1"].filter(Boolean).join(" ")}
            >
              {/* Hito */}
              <div className="flex flex-col items-center gap-1.5">
                {/* Círculo */}
                <div
                  aria-current={isCurrent ? "step" : undefined}
                  className={[
                    "flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full text-xs font-semibold transition-colors",
                    isCompleted
                      ? "bg-vk-blue text-white"
                      : isCurrent
                        ? "bg-vk-blue text-white ring-4 ring-vk-blue/20"
                        : "bg-vk-border-w text-vk-text-muted",
                  ].join(" ")}
                >
                  {isCompleted ? <CheckIcon /> : <span>{milestone.step}</span>}
                </div>

                {/* Label */}
                <span
                  className={[
                    "hidden text-xs md:block whitespace-nowrap",
                    isCompleted || isCurrent
                      ? isCurrent
                        ? "font-semibold text-vk-text-primary"
                        : "text-vk-text-secondary"
                      : "text-vk-text-muted",
                  ].join(" ")}
                >
                  {milestone.label}
                </span>
              </div>

              {/* Línea conectora */}
              {!isLast && (
                <div
                  aria-hidden="true"
                  className={[
                    "mx-2 mb-5 h-0.5 flex-1 transition-colors duration-300",
                    isCompleted ? "bg-vk-blue" : "bg-vk-border-w",
                  ].join(" ")}
                />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
