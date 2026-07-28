/**
 * Rubros (verticales) de Véktor — fuente única de verdad del frontend.
 *
 * Espejo de `backend/app/domain/verticals.py`. Dos reglas que este módulo
 * sostiene:
 *
 * 1. **Solo códigos LARGOS.** `kiosco_almacen`, `decoracion_hogar`, `limpieza`.
 *    El código corto legado (`"kiosco"`) NO existe acá: el backend lo rechaza
 *    (`VERTICAL_CODE_PATTERN` se deriva del enum y no aliasea nada), así que
 *    mantenerlo del lado del cliente solo produciría 422 silenciosos.
 * 2. **`otros` nunca es un vertical operativo.** Existe únicamente como opción
 *    del formulario público de solicitud de acceso (`RequestedVertical`): un
 *    negocio que no encaja en los tres rubros de hoy. El vertical operativo lo
 *    asigna el dueño al aprobar la solicitud.
 *
 * Las tarjetas (nombre + descripción + ícono) vienen de
 * `features/onboarding/Step1Vertical.tsx`; los íconos, verbatim, viven en
 * `components/ui/VerticalIcons.tsx` para que este archivo siga siendo datos
 * puros y lo pueda importar cualquier consumidor (formulario de solicitud,
 * /rubros, /contacto).
 */

import type { ComponentType } from "react";

import {
  IconHogar,
  IconKiosco,
  IconLimpieza,
  IconOtro,
} from "@/components/ui/VerticalIcons";

/** Verticales operativos: los únicos persistibles en un negocio dado de alta. */
export type Vertical = "kiosco_almacen" | "decoracion_hogar" | "limpieza";

/** Superconjunto elegible en la solicitud de acceso. */
export type RequestedVertical = Vertical | "otros";

export interface VerticalOption {
  code: RequestedVertical;
  name: string;
  description: string;
  /** Componente, no elemento: así este módulo no necesita JSX. */
  Icon: ComponentType;
}

/**
 * Los tres rubros soportados. El orden define el orden de render y es el mismo
 * que usa el sitio público (kiosco → limpieza → decoración).
 */
export const VERTICAL_OPTIONS: readonly VerticalOption[] = [
  {
    code: "kiosco_almacen",
    name: "Kiosco / Almacén",
    description: "Bebidas, golosinas, cigarrillos y productos de reventa rápida.",
    Icon: IconKiosco,
  },
  {
    code: "limpieza",
    name: "Limpieza",
    description: "Productos de limpieza, higiene y cuidado del hogar.",
    Icon: IconLimpieza,
  },
  {
    code: "decoracion_hogar",
    name: "Decoración del hogar",
    description: "Muebles, textiles y accesorios para el hogar.",
    Icon: IconHogar,
  },
] as const;

/** Opción exclusiva del formulario de solicitud: no es un vertical operativo. */
export const OTHER_VERTICAL_OPTION: VerticalOption = {
  code: "otros",
  name: "Otro",
  description: "Tu negocio es de otra cosa: contanos de qué y lo miramos.",
  Icon: IconOtro,
};

/** Las cuatro opciones que muestra el formulario de solicitud de acceso. */
export const REQUESTED_VERTICAL_OPTIONS: readonly VerticalOption[] = [
  ...VERTICAL_OPTIONS,
  OTHER_VERTICAL_OPTION,
] as const;

/** Códigos operativos, para validaciones y enums de Zod. */
export const VERTICAL_CODES = VERTICAL_OPTIONS.map((o) => o.code) as readonly Vertical[];

/** Códigos elegibles en la solicitud (los tres operativos + `otros`). */
export const REQUESTED_VERTICAL_CODES = REQUESTED_VERTICAL_OPTIONS.map(
  (o) => o.code,
) as readonly RequestedVertical[];

/** Nombre visible de un rubro; el código crudo si no se conoce (nunca inventa). */
export function verticalLabel(code: string | null | undefined): string {
  return REQUESTED_VERTICAL_OPTIONS.find((o) => o.code === code)?.name ?? (code ?? "");
}
