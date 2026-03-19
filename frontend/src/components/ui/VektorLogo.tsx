/**
 * VektorLogo — componente de branding inline (SVG, sin imágenes)
 *
 * Props:
 *   variant: 'full' | 'icon' | 'wordmark'
 *   size:    'sm' (20px) | 'md' (32px) | 'lg' (48px)
 *   theme:   'dark' | 'light'
 *
 * Variantes:
 *   'full'     — ícono V con escalera + texto "VÉKTOR"
 *   'icon'     — solo el ícono SVG
 *   'wordmark' — solo el texto "VÉKTOR"
 *
 * Theme:
 *   'light' — brazo izquierdo: navy (#1A2744), escalera+flecha: blue (#2B7FD4)
 *   'dark'  — ambos elementos en blanco (#FFFFFF)
 */

export type LogoVariant = "full" | "icon" | "wordmark";
export type LogoSize = "sm" | "md" | "lg";
export type LogoTheme = "dark" | "light";

interface VektorLogoProps {
  variant?: LogoVariant;
  size?: LogoSize;
  theme?: LogoTheme;
  className?: string;
}

// Altura base del ícono; ancho se calcula por el aspect ratio del viewBox (110:90)
const SIZE_PX: Record<LogoSize, number> = {
  sm: 20,
  md: 32,
  lg: 48,
};

// Tamaño del wordmark relativo al ícono
const WORDMARK_SIZE: Record<LogoSize, string> = {
  sm: "text-sm",
  md: "text-xl",
  lg: "text-3xl",
};

// Gap entre ícono y texto según size
const WORDMARK_GAP: Record<LogoSize, string> = {
  sm: "gap-1.5",
  md: "gap-2",
  lg: "gap-3",
};

/**
 * Ícono SVG: V con brazo izquierdo sólido navy + escalera de pasos azules
 * subiendo hacia la derecha, rematada con flecha triangular.
 *
 * ViewBox: 0 0 110 90
 * - Brazo izquierdo (navy/white): triángulo (6,8)→(30,8)→(46,80)
 * - Escalón 1 (blue/white): paralelo­grama diagonal inferior
 * - Escalón 2 (blue/white): paralelo­grama diagonal medio
 * - Escalón 3 (blue/white): paralelo­grama diagonal superior
 * - Cabeza de flecha (blue/white): triángulo apuntando arriba-derecha
 */
function VektorIcon({
  size,
  theme,
}: {
  size: LogoSize;
  theme: LogoTheme;
}) {
  const heightPx = SIZE_PX[size];
  // Mantener proporción 110:90 del viewBox
  const widthPx = Math.round((heightPx * 110) / 90);

  const bodyColor = theme === "dark" ? "#FFFFFF" : "#1A2744";
  const arrowColor = theme === "dark" ? "#FFFFFF" : "#2B7FD4";

  return (
    <svg
      width={widthPx}
      height={heightPx}
      viewBox="0 0 110 90"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {/* Brazo izquierdo de la V — triángulo sólido navy */}
      <polygon points="6,8 30,8 46,80" fill={bodyColor} />
      {/* Escalón 1 — inferior */}
      <polygon points="52,83 42,76 54,59 64,66" fill={arrowColor} />
      {/* Escalón 2 — medio */}
      <polygon points="65,65 54,58 67,41 77,48" fill={arrowColor} />
      {/* Escalón 3 — superior */}
      <polygon points="79,47 69,40 82,23 91,30" fill={arrowColor} />
      {/* Cabeza de flecha */}
      <polygon points="97,12 75,18 98,35" fill={arrowColor} />
    </svg>
  );
}

function VektorWordmark({
  size,
  theme,
}: {
  size: LogoSize;
  theme: LogoTheme;
}) {
  const textColor = theme === "dark" ? "text-white" : "text-vk-navy";

  return (
    <span
      className={[
        WORDMARK_SIZE[size],
        "font-bold tracking-tight leading-none select-none",
        textColor,
      ].join(" ")}
    >
      VÉKTOR
    </span>
  );
}

export function VektorLogo({
  variant = "full",
  size = "md",
  theme = "light",
  className,
}: VektorLogoProps) {
  if (variant === "icon") {
    return (
      <span className={className}>
        <VektorIcon size={size} theme={theme} />
      </span>
    );
  }

  if (variant === "wordmark") {
    return (
      <span className={className}>
        <VektorWordmark size={size} theme={theme} />
      </span>
    );
  }

  // full
  return (
    <span className={["inline-flex items-center", WORDMARK_GAP[size], className].filter(Boolean).join(" ")}>
      <VektorIcon size={size} theme={theme} />
      <VektorWordmark size={size} theme={theme} />
    </span>
  );
}
