import {
  ShoppingCart,
  HeartPulse,
  Boxes,
  Receipt,
  Link2,
  MessageSquare,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { PageHeader } from "@/components/public/PageHeader";

export const metadata = {
  title: "Tutoriales | Véktor",
  description:
    "Aprendé a sacarle el jugo a Véktor: cargar ventas, entender tu score de salud, controlar el stock y más.",
};

const tutoriales: {
  icon: LucideIcon;
  title: string;
  description: string;
}[] = [
  {
    icon: ShoppingCart,
    title: "Cargar tus ventas",
    description:
      "Registrá ventas desde el chat o subiendo un archivo, sin planillas.",
  },
  {
    icon: HeartPulse,
    title: "Entender tu score de salud",
    description:
      "Qué mide el score y cómo leer si tu negocio está sano o en riesgo.",
  },
  {
    icon: Boxes,
    title: "Controlar el stock",
    description:
      "Seguí tu inventario, detectá quiebres y evitá quedarte sin mercadería.",
  },
  {
    icon: Receipt,
    title: "Registrar gastos",
    description:
      "Cargá gastos y compras para conocer tu margen y tu caja reales.",
  },
  {
    icon: Link2,
    title: "Conectar Google",
    description:
      "Vinculá tus herramientas de Google para importar y exportar datos.",
  },
  {
    icon: MessageSquare,
    title: "Usar el chat con IA",
    description:
      "Preguntale a Véktor por tu negocio y pedile análisis en tu idioma.",
  },
];

export default function TutorialesPage() {
  return (
    <>
      <PageHeader
        title="Tutoriales"
        subtitle={
          <>
            Guías cortas para aprender a sacarle el jugo a Véktor desde el
            primer día.
          </>
        }
      />

      <section className="mx-auto max-w-5xl px-6 pb-24">
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {tutoriales.map(({ icon: Icon, title, description }) => (
            <div key={title} className="vektor-card flex flex-col p-6">
              <div className="mb-4 flex items-center justify-between">
                <span className="flex h-11 w-11 items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal">
                  <Icon className="h-5 w-5 text-white" />
                </span>
                <span className="rounded-full border border-white/15 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-vektor-muted">
                  Próximamente
                </span>
              </div>
              <h2 className="font-display text-xl font-bold uppercase tracking-tight text-vektor-white">
                {title}
              </h2>
              <p className="mt-2 text-vektor-body">{description}</p>
            </div>
          ))}
        </div>
      </section>
    </>
  );
}
