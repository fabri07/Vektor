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
 *    negocio que no encaja en los rubros de hoy. El vertical operativo lo
 *    asigna el dueño al aprobar la solicitud.
 *
 * Los seis son de REVENTA (comprar un producto y venderlo). Los rubros que
 * transforman materia prima —carnicería, pollería, panadería, rotisería— no
 * están: el motor asume `compra → producto → venta` y no tiene concepto de
 * rendimiento ni de receta. Espejo de la nota en `app/domain/verticals.py`.
 *
 * Las tarjetas (nombre + descripción + ícono) vienen de
 * `features/onboarding/Step1Vertical.tsx`; los íconos, verbatim, viven en
 * `components/ui/VerticalIcons.tsx` para que este archivo siga siendo datos
 * puros y lo pueda importar cualquier consumidor (formulario de solicitud,
 * /rubros, /contacto).
 *
 * El copy largo de cada rubro NO vive acá: está en `lib/rubros.ts`, que importa
 * este módulo. La separación es a propósito — este archivo lo importa el
 * esquema Zod del formulario, y no tiene por qué arrastrar texto de marketing.
 */

import type { ComponentType } from "react";

import {
  IconHogar,
  IconIndumentaria,
  IconKiosco,
  IconLibreria,
  IconLimpieza,
  IconOtro,
  IconVerduleria,
} from "@/components/ui/VerticalIcons";

/** Verticales operativos: los únicos persistibles en un negocio dado de alta. */
export type Vertical =
  | "kiosco_almacen"
  | "decoracion_hogar"
  | "limpieza"
  | "libreria_papeleria"
  | "indumentaria"
  | "verduleria_fruteria";

/** Superconjunto elegible en la solicitud de acceso. */
export type RequestedVertical = Vertical | "otros";

export interface VerticalOption {
  code: RequestedVertical;
  name: string;
  description: string;
  /** Componente, no elemento: así este módulo no necesita JSX. */
  Icon: ComponentType;
  /**
   * Ancla de la sección del rubro en `/rubros`. Es la MISMA que usa el menú
   * `Rubros` del nav público, que antes repetía la lista a mano. Los tres
   * primeros valores son los que ya existían (`#kiosco`, `#limpieza`, `#deco`):
   * están enlazados desde afuera, así que no se renombran.
   */
  anchor: string;
}

/**
 * Los seis rubros soportados. El orden define el orden de render en todos los
 * consumidores: los tres primeros conservan el orden que ya usaba el sitio
 * público (kiosco → limpieza → decoración) y los nuevos van después.
 */
export const VERTICAL_OPTIONS: readonly VerticalOption[] = [
  {
    code: "kiosco_almacen",
    name: "Kiosco / Almacén",
    description: "Productos de alta rotación y reposición frecuente.",
    Icon: IconKiosco,
    anchor: "kiosco",
  },
  {
    code: "limpieza",
    name: "Limpieza",
    description: "Productos de higiene, limpieza y cuidado del hogar.",
    Icon: IconLimpieza,
    anchor: "limpieza",
  },
  {
    code: "decoracion_hogar",
    name: "Decoración del hogar",
    description: "Muebles, textiles y objetos con fuerte estacionalidad.",
    Icon: IconHogar,
    anchor: "deco",
  },
  {
    code: "libreria_papeleria",
    name: "Librería y papelería",
    description: "Útiles, libros, mochilas y regalería con curva de temporada.",
    Icon: IconLibreria,
    anchor: "libreria",
  },
  {
    code: "indumentaria",
    name: "Indumentaria",
    description: "Ropa o calzado por temporada, talle y color.",
    Icon: IconIndumentaria,
    anchor: "indumentaria",
  },
  {
    code: "verduleria_fruteria",
    name: "Verdulería y frutería",
    description: "Productos frescos con rotación de pocos días.",
    Icon: IconVerduleria,
    anchor: "verduleria",
  },
] as const;

/** Opción exclusiva del formulario de solicitud: no es un vertical operativo. */
export const OTHER_VERTICAL_OPTION: VerticalOption = {
  code: "otros",
  name: "Otro",
  description: "Contanos la actividad y evaluamos cómo puede adaptarse Véktor.",
  Icon: IconOtro,
  // No tiene sección en /rubros — no es un rubro, es la ausencia de uno.
  anchor: "",
};

/** Las opciones que muestra el formulario de solicitud de acceso (los 6 + "Otro"). */
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
