/**
 * Íconos de los rubros (verticales) soportados por Véktor.
 *
 * Kiosco, Hogar y Limpieza vienen TAL CUAL de `features/onboarding/Step1Vertical.tsx`
 * —mismo `viewBox`, mismo `strokeWidth`, mismos paths—: se mudaron acá para que
 * `lib/verticals.ts` pueda ser un módulo de datos puro (`.ts`, sin JSX) y
 * referenciar el componente en vez de instanciarlo.
 *
 * Librería, Indumentaria y Verdulería se sumaron con la ampliación a 6 rubros,
 * escritos en el mismo sistema (24×24, trazo 1.75, puntas redondeadas). No son
 * los doodles: esos viven en `components/public/doodles.tsx`, miden 240 y llevan
 * trazo 6. Estos son íconos de UI —van en el selector del formulario, a 24px—.
 *
 * `IconOtro` acompaña a la última opción del formulario de solicitud de acceso
 * ("Otro"), que no es un vertical operativo.
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

export function IconLibreria() {
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
      <path d="M4 4.5A2.5 2.5 0 016.5 2H20v18H6.5A2.5 2.5 0 014 17.5z" />
      <path d="M4 17.5A2.5 2.5 0 016.5 15H20" />
    </svg>
  );
}

export function IconIndumentaria() {
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
      <path d="M9 3L4.5 5 3 9.5l3 1V21h12V10.5l3-1L19.5 5 15 3" />
      <path d="M9 3a3 3 0 006 0" />
    </svg>
  );
}

export function IconVerduleria() {
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
      <path d="M3 9h18l-1.8 11H4.8L3 9z" />
      <path d="M8.5 9v11M15.5 9v11" />
      <path d="M6.5 9a3 3 0 016 0" />
      <path d="M13 9a2.5 2.5 0 015 0" />
    </svg>
  );
}

/** Rubro fuera de los rubros soportados: se describe con texto libre. */
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
