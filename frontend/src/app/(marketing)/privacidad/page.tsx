import { AlertTriangle } from "lucide-react";
import { PageHeader } from "@/components/public/PageHeader";

export const metadata = {
  title: "Política de privacidad | Véktor",
  description:
    "Política de privacidad de Véktor (borrador) según la Ley 25.326 de Protección de Datos Personales y la AAIP.",
};

export default function PrivacidadPage() {
  return (
    <>
      <PageHeader
        title="Política de privacidad"
        subtitle={<>Cómo tratamos y protegemos tus datos personales.</>}
      />

      <section className="mx-auto max-w-3xl px-6 pb-24">
        <div className="vektor-card border-vektor-amber/40 p-6">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-vektor-amber" />
            <p className="text-vektor-amber">
              Este es un <strong>borrador</strong> pendiente de revisión legal.
              El texto definitivo puede cambiar antes de su publicación oficial.
            </p>
          </div>
        </div>

        <p className="mt-6 text-sm text-vektor-muted">
          Última actualización: Julio 2026
        </p>

        <div className="mt-8 space-y-10">
          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Qué datos recopilamos
            </h2>
            <p className="mt-4 text-vektor-body">
              Recopilamos los datos que necesitás para usar Véktor y los que nos
              das voluntariamente. Esto incluye datos de tu cuenta (nombre,
              email y datos de tu negocio) y la información financiera que
              cargás dentro de la plataforma.
            </p>
            <p className="mt-4 text-vektor-body">
              Si completás nuestro formulario de contacto, guardamos los datos
              que ingresás: nombre, celular, email, empresa, rubro, cantidad de
              usuarios y cómo gestionás tu negocio.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Para qué los usamos
            </h2>
            <p className="mt-4 text-vektor-body">
              Usamos tus datos para contactarte, responder tus consultas,
              prestarte el servicio y mejorarlo. No vendemos tu información
              personal ni la usamos para fines ajenos a los descritos en esta
              política.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Formulario de contacto y consentimiento
            </h2>
            <p className="mt-4 text-vektor-body">
              Cuando completás el formulario de contacto, registramos tu
              consentimiento junto con la versión del texto que aceptaste y la
              fecha en que lo hiciste. Podés retirar tu consentimiento en
              cualquier momento escribiéndonos, y dejaremos de usar tus datos
              para esos fines.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Datos técnicos
            </h2>
            <p className="mt-4 text-vektor-body">
              Por razones de seguridad, al enviar el formulario guardamos un
              <strong> hash</strong> (una huella irreversible) de tu dirección
              IP, con una retención limitada. No almacenamos la IP cruda: el
              hash solo se usa para prevenir abuso y spam, y no permite
              reconstruir tu dirección real.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Terceros
            </h2>
            <p className="mt-4 text-vektor-body">
              Para enviar emails transaccionales (por ejemplo, confirmaciones o
              respuestas a tu consulta) usamos el servicio Resend. Estos
              proveedores procesan únicamente los datos necesarios para prestar
              ese servicio.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Tus derechos
            </h2>
            <p className="mt-4 text-vektor-body">
              De acuerdo con la Ley 25.326 de Protección de Datos Personales,
              tenés derecho a acceder a tus datos, rectificarlos y solicitar su
              supresión. Para ejercer estos derechos, escribinos. También podés
              presentar reclamos ante la Agencia de Acceso a la Información
              Pública (AAIP), autoridad de aplicación en Argentina.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Contacto
            </h2>
            <p className="mt-4 text-vektor-body">
              Si tenés dudas sobre esta política o querés ejercer tus derechos,
              podés escribirnos a través de nuestro formulario de contacto y te
              vamos a responder a la brevedad.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
