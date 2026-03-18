export type Vertical = "kiosco" | "decoracion_hogar" | "limpieza";

interface VerticalOption {
  code: Vertical;
  name: string;
  description: string;
  icon: React.ReactNode;
}

function IconKiosco() {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z" />
      <polyline points="9 22 9 12 15 12 15 22" />
    </svg>
  );
}

function IconHogar() {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M3 9h18" />
      <path d="M9 21V9" />
    </svg>
  );
}

function IconLimpieza() {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M8 2v4" />
      <path d="M16 2v4" />
      <path d="M3 10h18" />
      <path d="M5 6h14a2 2 0 012 2v12a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2z" />
      <path d="M9 14h6" />
    </svg>
  );
}

const VERTICALS: VerticalOption[] = [
  {
    code: "kiosco",
    name: "Kiosco",
    description: "Bebidas, golosinas, cigarrillos y productos de reventa rápida.",
    icon: <IconKiosco />,
  },
  {
    code: "decoracion_hogar",
    name: "Decoración del Hogar",
    description: "Muebles, textiles y accesorios para el hogar.",
    icon: <IconHogar />,
  },
  {
    code: "limpieza",
    name: "Limpieza",
    description: "Productos de limpieza, higiene y cuidado del hogar.",
    icon: <IconLimpieza />,
  },
];

interface Step1VerticalProps {
  selected: Vertical | null;
  onSelect: (v: Vertical) => void;
}

export function Step1Vertical({ selected, onSelect }: Step1VerticalProps) {
  return (
    <div>
      <h2 className="mb-1 text-xl font-semibold text-gray-900">
        ¿Qué tipo de negocio tenés?
      </h2>
      <p className="mb-8 text-sm text-gray-500">
        Seleccioná el rubro que mejor describe tu actividad.
      </p>

      <div className="grid gap-4 sm:grid-cols-3">
        {VERTICALS.map((v) => {
          const isSelected = selected === v.code;
          return (
            <button
              key={v.code}
              type="button"
              onClick={() => onSelect(v.code)}
              className={[
                "flex flex-col items-start gap-4 rounded-xl border-2 p-5 text-left transition-all duration-150",
                isSelected
                  ? "border-[#E63946] bg-red-50 shadow-sm"
                  : "border-gray-200 bg-white hover:border-gray-300 hover:shadow-sm",
              ].join(" ")}
            >
              <span
                className={[
                  "inline-flex h-11 w-11 items-center justify-center rounded-xl transition-colors",
                  isSelected
                    ? "bg-vk-blue text-white"
                    : "bg-vk-border-w text-vk-text-secondary",
                ].join(" ")}
              >
                {v.icon}
              </span>
              <div>
                <p className="font-semibold text-gray-900">{v.name}</p>
                <p className="mt-1 text-sm leading-snug text-gray-500">
                  {v.description}
                </p>
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
