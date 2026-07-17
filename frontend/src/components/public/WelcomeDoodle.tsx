/**
 * WelcomeDoodle — doodle de bienvenida para el panel izquierdo de /register.
 *
 * A diferencia de `DoodleCollage` (mosaico de 6 doodles compartido entre login
 * y la landing), acá va un único doodle nuevo (`DoodleNegocio`) más el mensaje
 * "Bienvenido a Véktor" — que NO es texto: son trazos SVG que se dibujan solos,
 * igual que el doodle de arriba. Reusa las mismas clases (`.doodle-anim` /
 * `.doodle-path`, definidas en globals.css) para heredar el dibujado del trazo,
 * el fade + float y el respeto por `prefers-reduced-motion`.
 *
 * El lettering vive acá y no en `doodles.tsx` porque rompe el contrato de ese
 * archivo (viewBox 240x240 uniforme, trazo blanco): usa viewBox propio y dos
 * colores. Si algún día se reusa en otra pantalla, moverlo.
 *
 * Las letras son line-art escrito a mano, no una fuente convertida a paths: las
 * verticales ondulan apenas y cada letra va rotada 1–2° para que se lea como
 * trazo y no como tipografía redondeada.
 */

import { DoodleNegocio } from "./doodles";

/** Una letra = sus trazos + el jitter que la despega de la grilla. */
type Letter = { rotate: string; paths: readonly string[] };

const LINEA_BIENVENIDO: readonly Letter[] = [
  {
    rotate: "rotate(-2 25 31)",
    paths: [
      "M16 45C15 36 17 26 16 17",
      "M16 17C21 16.5 26 16 29 18C33 20 33 27 28 29C24 30 19 30 16 30",
      "M16 30C21 29.5 27 30 31 31C36 33 36 42 30 44C25 45.5 20 45 16 45",
    ],
  },
  { rotate: "rotate(2 41 33)", paths: ["M41 45C40 40 41.5 34 41 29", "M41 20V20.5"] },
  {
    rotate: "rotate(-1 57 37)",
    paths: [
      "M49 37C54 36.5 60 37 65 37C66 32 61 28.5 56.5 29C51.5 29.5 48.5 33 49 37.5C49.5 42 53 45.5 57.5 45C60.5 44.7 63 44 65 42",
    ],
  },
  {
    rotate: "rotate(1 80 37)",
    paths: [
      "M72 45C71 40 72.5 34 72 29",
      "M72 35C74 31 77 28.5 81 29C85 29.5 87.5 32.5 87 36.5C86.7 40 87 42.5 87 45",
    ],
  },
  { rotate: "rotate(-2 100 37)", paths: ["M93 29C95 34 97.5 40 100 45.5C102.5 40 105 34 107 29"] },
  {
    rotate: "rotate(1 121 37)",
    paths: [
      "M113 37C118 36.5 124 37 129 37C130 32 125 28.5 120.5 29C115.5 29.5 112.5 33 113 37.5C113.5 42 117 45.5 121.5 45C124.5 44.7 127 44 129 42",
    ],
  },
  {
    rotate: "rotate(-1 144 37)",
    paths: [
      "M136 45C135 40 136.5 34 136 29",
      "M136 35C138 31 141 28.5 145 29C149 29.5 151.5 32.5 151 36.5C150.7 40 151 42.5 151 45",
    ],
  },
  { rotate: "rotate(2 159 33)", paths: ["M159 45C158 40 159.5 34 159 29", "M159 20V20.5"] },
  {
    rotate: "rotate(-1 175 31)",
    paths: [
      "M183 37C182.5 32 178 28.5 174 29C169 29.5 166.5 33 167 37.5C167.5 42 171 45.5 175.5 45C179.5 44.5 183 41.5 183 37",
      "M183 17C184 26 182.5 36 183 45",
    ],
  },
  {
    rotate: "rotate(1 199 37)",
    paths: [
      "M199 29C194 28.5 190.5 32.5 191 37C191.5 41.5 195 45.5 199.5 45C204 44.5 207.5 41 207 36.5C206.5 32.5 203 29 199 29Z",
    ],
  },
  {
    rotate: "rotate(-2 230 37)",
    paths: [
      "M238 37C237.5 32.5 233 28.5 229 29C224 29.5 221.5 33 222 37.5C222.5 42 226 45.5 230.5 45C234.5 44.5 238 41.5 238 37",
      "M238 29C239 34 237.5 40 238 45",
    ],
  },
];

const LINEA_VEKTOR: readonly Letter[] = [
  { rotate: "rotate(-2 55 95)", paths: ["M37 75C42 88 48 101 55 115.5C61.5 101 67.5 88 73 75"] },
  {
    rotate: "rotate(1 95 100)",
    paths: [
      "M84 104C91 103.5 99 104 106 104C107 98 101.5 93.5 95 94C87.5 94.5 83.5 99 84 105.5C84.5 111.5 89 115.5 95.5 115C99.5 114.7 103 113 105 110",
      "M92 87.5C95 84.5 97.5 81.5 100.5 79",
    ],
  },
  {
    rotate: "rotate(-1 126 95)",
    paths: [
      "M115 115C114 101 116 87 115 73",
      "M137 94C131 98.5 124 103.5 118 108",
      "M126 102C130.5 106.5 135 111 139.5 115",
    ],
  },
  {
    rotate: "rotate(2 154 100)",
    paths: [
      "M152 80C151 89 152.5 99 152 108C152 112.5 154.5 115.5 158 115C160 114.8 162 114 163 113",
      "M145 94C150.5 93.5 157 94 162.5 94",
    ],
  },
  {
    rotate: "rotate(-1 184 105)",
    paths: [
      "M184 94C177.5 93.5 172 98.5 172 105C172 111.5 177 115.5 184 115C191 114.5 196.5 111 196 104.5C195.5 98.5 190.5 94 184 94Z",
    ],
  },
  {
    rotate: "rotate(1 214 105)",
    paths: [
      "M205 115C204 108 205.5 101 205 94",
      "M205 102C206.5 97 211 93.5 216.5 94C219.5 94.3 221.5 95 223 96.5",
    ],
  },
];

/** El doodle de arriba lanza su último trazo a los 0.96s; el mensaje entra atrás. */
const LETTER_START_S = 1.1;
/** Un trazo cada 45ms: se lee como alguien escribiendo, no como un fade masivo. */
const LETTER_STEP_S = 0.045;

/**
 * Aplana las letras a <path> numerando el delay de forma corrida (`from` sigue
 * la cuenta entre líneas), para que el escalonado no se reinicie en cada letra
 * ni en el salto de línea.
 */
function LetterGroup({ letters, from }: { letters: readonly Letter[]; from: number }) {
  let i = from;
  return (
    <>
      {letters.map((letter, li) => (
        <g key={li} transform={letter.rotate}>
          {letter.paths.map((d) => (
            <path
              key={d}
              d={d}
              pathLength={1}
              className="doodle-path"
              style={{ animationDelay: `${(LETTER_START_S + i++ * LETTER_STEP_S).toFixed(3)}s` }}
            />
          ))}
        </g>
      ))}
    </>
  );
}

const BIENVENIDO_PATHS = LINEA_BIENVENIDO.reduce((n, l) => n + l.paths.length, 0);

export function WelcomeDoodle({ className = "" }: { className?: string }) {
  return (
    <div className={`flex flex-col items-center justify-center gap-5 ${className}`}>
      <div className="doodle-anim w-full max-w-[220px]">
        <DoodleNegocio />
      </div>

      {/* Dibujo, no texto — de ahí el role/aria-label: el mensaje se sigue
          anunciando en lectores de pantalla. */}
      <svg
        viewBox="0 0 256 130"
        fill="none"
        role="img"
        aria-label="Bienvenido a Véktor"
        className="h-auto w-full max-w-[280px]"
      >
        <g strokeWidth={5} strokeLinecap="round" strokeLinejoin="round">
          <g stroke="rgb(232 237 244)">
            <LetterGroup letters={LINEA_BIENVENIDO} from={0} />
          </g>
          <g stroke="rgb(43 127 212)" strokeWidth={6}>
            <LetterGroup letters={LINEA_VEKTOR} from={BIENVENIDO_PATHS} />
          </g>
        </g>
      </svg>
    </div>
  );
}
