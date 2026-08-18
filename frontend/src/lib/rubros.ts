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
    tension: "Que el movimiento diario no te tape el margen.",
    respuesta:
      "Véktor conecta cada compra y cada venta para que sepas qué falta y qué te deja plata de verdad. El stock se actualiza con el movimiento del negocio, sin rehacer cuentas a mano.",
    capacidades: [
      "Cerrá la caja con diferencias a la vista",
      "Reponé antes de perder la venta",
      "Priorizá productos por margen y rotación",
    ],
  },
  limpieza: {
    tension: "Vendiste. Ahora necesitás saber cuándo esa venta se vuelve plata.",
    respuesta:
      "Véktor separa mercadería, gastos y cuentas por cobrar para mostrarte el resultado real y el efectivo disponible. Así podés cobrar a tiempo, cuidar la caja y defender el margen.",
    capacidades: [
      "Deudas ordenadas por cliente y antigüedad",
      "Costos de mercadería separados de la estructura",
      "Rentabilidad visible por línea de producto",
    ],
  },
  decoracion_hogar: {
    tension: "Lo que no rota también ocupa caja.",
    respuesta:
      "Véktor identifica qué productos sostienen las ventas y cuáles inmovilizan capital. Con costos y proveedores comparados, decidís qué reponer, liquidar o dejar de comprar.",
    capacidades: [
      "Capital inmovilizado, detectado a tiempo",
      "Proveedores y costos comparables",
      "Precios respaldados por el margen real",
    ],
  },
  libreria_papeleria: {
    tension: "La temporada dura semanas. Sus decisiones impactan todo el año.",
    respuesta:
      "Véktor distingue la zafra del movimiento permanente y registra la demanda por lista y colegio. Comprás la próxima temporada con evidencia, no con memoria.",
    capacidades: [
      "Stock separado por temporada y fecha clave",
      "Demanda histórica por lista y colegio",
      "Margen por útiles, mochilas, libros y regalería",
    ],
  },
  indumentaria: {
    tension: "Cada talle que queda tiene un costo.",
    respuesta:
      "Véktor traza temporada, talle, color, canal y tipo de venta para mostrarte qué rota y qué pierde valor. El margen separa precio pleno de liquidación para que el resultado no se disfrace.",
    capacidades: [
      "Rotación visible por temporada, talle y color",
      "Margen neto de liquidaciones",
      "Rendimiento comparado por canal",
    ],
  },
  verduleria_fruteria: {
    tension: "En productos frescos, decidir tarde cuesta mercadería.",
    respuesta:
      "Véktor compara lo que entra del mercado con lo que sale por caja, y suma el flete al costo real. Vas rápido sabiendo dónde ajustar precio, compra o rotación antes de que el producto pierda margen.",
    capacidades: [
      "Costo completo por unidad de compra",
      "Precio de mercado frente a precio de venta",
      "Margen por producto y calidad comercial",
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
