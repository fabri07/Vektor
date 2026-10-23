import { UPLOAD_SIZE_HINT } from "@/constants/upload";

/**
 * Nota aclaratoria del límite de tamaño de archivo (16 MB), para mostrar en cada
 * control de upload de la app. Por defecto usa texto blanco/primario (alto contraste
 * sobre fondo oscuro del tema). En fondos claros, pasar `className` con un color oscuro.
 */
export function UploadSizeHint({ className = "" }: { className?: string }) {
  return (
    <p className={`text-xs font-medium text-vk-text-primary ${className}`}>
      {UPLOAD_SIZE_HINT}
    </p>
  );
}
