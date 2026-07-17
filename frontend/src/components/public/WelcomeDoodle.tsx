/**
 * WelcomeDoodle — doodle de bienvenida para el panel izquierdo de /register.
 *
 * A diferencia de `DoodleCollage` (mosaico de 6 doodles compartido entre login
 * y la landing), acá va un único doodle nuevo (`DoodleNegocio`) más el mensaje
 * "Bienvenido a Véktor". Reusa las mismas clases de animación (`.doodle-anim`/
 * `.doodle-path`, definidas en globals.css) para heredar el dibujado del trazo,
 * el fade + float y el respeto por `prefers-reduced-motion`.
 */

import { DoodleNegocio } from "./doodles";

export function WelcomeDoodle({ className = "" }: { className?: string }) {
  return (
    <div className={`flex flex-col items-center justify-center gap-5 ${className}`}>
      <div className="doodle-anim w-full max-w-[220px]">
        <DoodleNegocio />
      </div>
      <p className="text-center font-display text-xl font-bold text-vk-text-light">
        Bienvenido a <span className="text-vk-blue">Véktor</span>
      </p>
    </div>
  );
}
