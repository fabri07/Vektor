/**
 * DoodleCollage — collage de los 6 doodles line-art blancos, compartido entre el
 * hero de la landing y el panel izquierdo del login.
 *
 * Los doodles son SVG inline (`./doodles`) generados en Figma. Se embeben inline
 * — no vía <Image> — para que el CSS pueda animar el "dibujado" del trazo
 * (`.doodle-path`) además del fade + flotar del contenedor (`.doodle-anim`).
 * Todo respeta `prefers-reduced-motion` (ver globals.css).
 *
 * Las posiciones son proporcionales (%), así que el collage escala a cualquier
 * ancho: el consumidor fija tamaño y centrado vía `className`.
 */

import {
  DoodleChat,
  DoodleCrecimiento,
  DoodleDeco,
  DoodleKiosco,
  DoodleLimpieza,
  DoodlePersonaLaptop,
} from "./doodles";

// Posiciones del collage (en %). `delay` escalona la entrada (fade) del doodle;
// `drawDelay` arranca el "dibujado" de sus trazos en sincronía con esa entrada.
const DOODLES = [
  { Comp: DoodlePersonaLaptop, style: "left-[4%] top-[6%] w-[44%]", delay: 0 },
  { Comp: DoodleKiosco, style: "right-[2%] top-[0%] w-[36%]", delay: 0.12 },
  { Comp: DoodleCrecimiento, style: "right-[6%] top-[42%] w-[32%]", delay: 0.24 },
  { Comp: DoodleLimpieza, style: "left-[0%] top-[52%] w-[30%]", delay: 0.36 },
  { Comp: DoodleDeco, style: "left-[34%] top-[58%] w-[32%]", delay: 0.48 },
  { Comp: DoodleChat, style: "right-[32%] top-[8%] w-[28%]", delay: 0.6 },
];

export function DoodleCollage({ className = "" }: { className?: string }) {
  return (
    <div aria-hidden className={`relative ${className}`}>
      {DOODLES.map(({ Comp, style, delay }, i) => (
        <div
          key={i}
          className={`doodle-anim absolute ${style}`}
          style={{ animationDelay: `${delay}s` }}
        >
          <Comp drawDelay={delay} />
        </div>
      ))}
    </div>
  );
}
