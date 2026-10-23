import { AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/public/PageHeader";

export const metadata = {
  title: "Términos y condiciones | Véktor",
  description:
    "Términos y condiciones de uso de Véktor (borrador). La versión completa se publicará próximamente.",
};

export default function TerminosPage() {
  return (
    <>
      <PageHeader
        title="Términos y condiciones"
        subtitle={<>Las reglas de uso de Véktor, en un lenguaje claro.</>}
      />

      <section className="mx-auto max-w-3xl px-6 pb-24">
        <div className="vektor-card border-vektor-amber/40 p-6">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-vektor-amber" />
            <p className="text-vektor-amber">
              Este es un <strong>borrador</strong>. Los términos completos se
              publicarán pronto.
            </p>
          </div>
        </div>

        <p className="mt-6 text-sm text-vektor-muted">
          Última actualización: Julio 2026
        </p>

        <div className="mt-8">
          <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
            Resumen de uso responsable
          </h2>
          <p className="mt-4 text-vektor-body">
            Estamos terminando de redactar los términos y condiciones
            definitivos. Mientras tanto, te pedimos un uso responsable de la
            plataforma: cargá información veraz de tu negocio, no la uses para
            fines ilegales ni para vulnerar derechos de terceros, y tené
            presente que Véktor es una herramienta de apoyo a la gestión, no un
            reemplazo del asesoramiento contable, legal o profesional.
          </p>
          <p className="mt-4 text-vektor-body">
            Cuando publiquemos la versión completa, vas a poder consultarla acá
            mismo. Si tenés dudas hasta entonces, escribinos por el formulario
            de contacto.
          </p>
        </div>
      </section>
    </>
  );
}
