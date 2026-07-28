/**
 * Íconos de los rubros (verticales) soportados por Véktor.
 *
 * Los tres primeros vienen TAL CUAL de `features/onboarding/Step1Vertical.tsx`
 * —mismo `viewBox`, mismo `strokeWidth`, mismos paths—: se mudaron acá para que
 * `lib/verticals.ts` pueda ser un módulo de datos puro (`.ts`, sin JSX) y
 * referenciar el componente en vez de instanciarlo.
 *
 * `IconOtro` es el único agregado: acompaña a la 4ª opción del formulario de
 * solicitud de acceso ("Otro"), que no es un vertical operativo.
 */

export function IconKiosco() {
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

export function IconHogar() {
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

export function IconLimpieza() {
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

/** Rubro fuera de los tres soportados: se describe con texto libre. */
export function IconOtro() {
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
      <circle cx="12" cy="12" r="9" />
      <path d="M9.5 9.5a2.5 2.5 0 115 0c0 1.5-2.5 1.75-2.5 3.5" />
      <path d="M12 17h.01" />
    </svg>
  );
}
