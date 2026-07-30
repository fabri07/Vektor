/**
 * Contenido de `/rubros`: qué le dice Véktor a cada rubro.
 *
 * Vive separado de `lib/verticals.ts` a propósito. Ese módulo lo importa el
 * esquema Zod del formulario de solicitud, y no tiene por qué arrastrar párrafos
 * de marketing hasta el bundle de un `<select>`. Acá se le suma copy a la
 * identidad que define aquel: el código, el nombre y el ancla salen de allá, y
 * este archivo no puede inventar un rubro que no exista.
 *
 * **Las capacidades describen lo que el producto hace hoy.** Cada una es
 * verificable contra un campo o un cálculo real:
 *
 * - Los campos por rubro salen de `backend/app/application/data/vertical_fields/`
 *   (temporada, talle, color, lista escolar, unidad de venta, calidad comercial,
 *   mercado de compra, costo de flete…).
 * - Los umbrales de rotación y margen, de `data/heuristics/<rubro>.json`.
 *
 * Lo que NO está implementado no se promete. El caso vivo es la **merma** de
 * verdulería: es la métrica que el rubro pide primero y está diferida a
 * propósito —necesita un motor de rendimiento que hoy no existe—, así que el
 * copy de verdulería habla de costo con flete y de rotación, que sí existen, y
 * no la menciona. Antes de sumar una capacidad acá, buscá el campo o la cuenta
 * que la sostiene; si no aparece, no va.
 */

import type { ComponentType } from "react";

import {
  DoodleDeco,
  DoodleIndumentaria,
  DoodleKiosco,
  DoodleLibreria,
  DoodleLimpieza,
  DoodleVerduleria,
} from "@/components/public/doodles";
import { VERTICAL_OPTIONS, type Vertical, type VerticalOption } from "@/lib/verticals";

export interface RubroContenido {
  /** La frase que reconoce el problema del rubro. Es el gancho, no el resumen. */
  tension: string;
  /** Cómo lo resuelve Véktor. Prosa, no lista. */
  respuesta: string;
  /** Tres capacidades concretas. Cada una tiene que existir en el producto. */
  capacidades: readonly string[];
}

/**
 * El doodle grande de cada rubro (240px, trazo 6). Distinto del `Icon` de
 * `VerticalOption`, que mide 24 y va en formularios: son dos sistemas de dibujo
 * con grosores distintos y no se pueden intercambiar sin que uno de los dos se
 * vea mal. Los doodles solo se usan en marketing, por eso viven acá y no en
 * `lib/verticals.ts`.
 */
const DOODLES: Record<Vertical, ComponentType<{ drawDelay?: number }>> = {
  kiosco_almacen: DoodleKiosco,
  limpieza: DoodleLimpieza,
  decoracion_hogar: DoodleDeco,
  libreria_papeleria: DoodleLibreria,
  indumentaria: DoodleIndumentaria,
  verduleria_fruteria: DoodleVerduleria,
};

const CONTENIDO: Record<Vertical, RubroContenido> = {
  kiosco_almacen: {
    tension: "Todo pasa por la caja y por la mercadería, y las dos se mueven todo el día.",
    respuesta:
      "Véktor te ordena las ventas del día, te avisa cuándo se te está por acabar un producto y te muestra qué margen real te deja cada cosa que vendés. Cargás una compra al proveedor y el stock se actualiza solo; vendés y el inventario baja.",
    capacidades: [
      "Cierre de caja diario con arqueo de efectivo",
      "Alertas de quiebre antes de quedarte sin el producto",
      "Margen por producto para saber qué te conviene reponer",
    ],
  },
  limpieza: {
    tension: "Vendés a cuenta, y el fiado no se ve en la caja hasta que ya es tarde.",
    respuesta:
      "Véktor separa lo que es costo de mercadería de lo que es gasto fijo, y te muestra qué clientes te deben y cuánto. Así sabés si estás ganando plata de verdad o si la cuenta corriente te está comiendo el efectivo.",
    capacidades: [
      "Cuenta corriente por cliente y antigüedad de la deuda",
      "Costo de insumos separado de los gastos fijos",
      "Margen por línea de producto",
    ],
  },
  decoracion_hogar: {
    tension: "Muchos productos distintos, y los que no se venden no avisan.",
    respuesta:
      "Véktor te muestra qué se vende bien, qué tenés parado ocupando plata y cómo vienen tus proveedores. Con eso decidís qué reponer, qué liquidar y a quién comprarle mejor.",
    capacidades: [
      "Detección de sobrestock y baja rotación",
      "Comparación de proveedores y precios de compra",
      "Margen por producto para ajustar precios",
    ],
  },
  libreria_papeleria: {
    tension: "Facturás fuerte dos veces al año, y el resto del tiempo sostenés.",
    respuesta:
      "Véktor separa lo de zafra escolar de lo que se vende todo el año, así ves cuánta de tu plata está parada esperando a marzo. Con la lista de cada colegio cargada en la venta, el año que viene comprás sabiendo qué te pidieron de verdad.",
    capacidades: [
      "Stock por temporada: zafra, permanente o fecha especial",
      "Ventas por lista escolar y por colegio",
      "Margen por línea: útiles, mochilas, libros, regalería",
    ],
  },
  indumentaria: {
    tension: "El problema no es vender: es lo que te queda sin vender.",
    respuesta:
      "Véktor te muestra qué se movió de la temporada y qué talles y colores quedaron colgados en la percha. Y distingue la venta de liquidación de la venta a precio lleno, para que el margen que ves sea el que de verdad te quedó.",
    capacidades: [
      "Rotación por temporada, talle y color",
      "Margen real separando liquidación de precio lleno",
      "Ventas por canal: local, online o retiro en local",
    ],
  },
  verduleria_fruteria: {
    tension: "Lo que traés del mercado se vende esta semana o no se vende.",
    respuesta:
      "Véktor ordena lo que comprás —por kilo, por atado o por cajón— contra lo que sale por caja, y le suma el flete al costo, que es donde se te escapa el margen cuando el cajón viene de lejos.",
    capacidades: [
      "Costo real por kilo, atado o cajón, con el flete adentro",
      "Precio del mercado contra tu precio de venta",
      "Margen por producto y por calidad comercial",
    ],
  },
};

export interface Rubro extends VerticalOption, RubroContenido {
  code: Vertical;
  Doodle: ComponentType<{ drawDelay?: number }>;
}

/**
 * Los seis rubros con su copy, en el orden de `VERTICAL_OPTIONS`.
 *
 * Los dos `Record` de arriba están tipados por `Vertical`, así que sumar un
 * rubro al enum sin escribirle el copy ni darle doodle NO COMPILA. Es el mismo
 * mecanismo que usa el backend con el JSON de heurísticas: un rubro a medio dar
 * de alta falla al construir, no en la cara del visitante.
 */
export const RUBROS: readonly Rubro[] = VERTICAL_OPTIONS.map((o) => {
  const code = o.code as Vertical;
  return { ...(o as VerticalOption & { code: Vertical }), ...CONTENIDO[code], Doodle: DOODLES[code] };
});
