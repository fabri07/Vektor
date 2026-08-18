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
        <p className="text-sm text-vektor-muted">
          Última actualización: Agosto 2026
        </p>

        <p className="mt-6 text-vektor-body">
          Véktor procesa información financiera de tu negocio: cuanto más
          confiés en cómo la tratamos, más útil te va a resultar la
          plataforma. Esta política explica, en un lenguaje directo, qué datos
          pedimos, para qué los usamos y qué control tenés sobre ellos.
        </p>

        <div className="mt-8 space-y-10">
          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Qué datos recopilamos
            </h2>
            <p className="mt-4 text-vektor-body">
              Para darte de alta y operar la cuenta, guardamos tus datos de
              contacto (nombre, email, teléfono) y los de tu negocio (rubro,
              tamaño, forma jurídica). Una vez adentro, guardamos también la
              información financiera que vos cargás o nos enviás — ventas,
              gastos, compras, stock y proveedores — porque es la base de todo
              lo que Véktor calcula.
            </p>
            <p className="mt-4 text-vektor-body">
              Antes de tener cuenta, si nos escribís por el formulario de
              contacto o pedís acceso, guardamos lo que completás ahí: nombre,
              teléfono, email, nombre del negocio, rubro y cómo gestionás hoy
              tu operación. Lo usamos únicamente para evaluar tu solicitud y
              responderte.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Para qué los usamos
            </h2>
            <p className="mt-4 text-vektor-body">
              Tus datos existen para prestarte el servicio: calcular tu salud
              financiera, responder tus consultas, avisarte cuando algo
              necesita tu atención y mejorar la plataforma a partir de cómo se
              usa. No los usamos para nada fuera de eso, y no vendemos tu
              información personal ni la de tu negocio a nadie.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Con quién la compartimos
            </h2>
            <p className="mt-4 text-vektor-body">
              Solo con los proveedores que necesitamos para que Véktor
              funcione, y solo con los datos indispensables para ese fin
              puntual: hoy, el envío de emails transaccionales (confirmaciones,
              respuestas a consultas, recuperación de contraseña) corre por
              Resend. No cedemos tu información a terceros con fines
              comerciales ni la usamos para publicidad.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Seguridad y datos técnicos
            </h2>
            <p className="mt-4 text-vektor-body">
              Al enviar un formulario público (contacto o solicitud de acceso)
              guardamos un <strong>hash</strong> —una huella irreversible— de tu
              dirección IP, por un plazo limitado, exclusivamente para prevenir
              abuso y spam automatizado. Nunca almacenamos la IP cruda, y ese
              hash no permite reconstruirla.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Tu consentimiento
            </h2>
            <p className="mt-4 text-vektor-body">
              Cuando completás un formulario público, registramos tu
              consentimiento junto con la versión del texto que aceptaste y la
              fecha exacta. Podés retirarlo en cualquier momento escribiéndonos:
              a partir de ese pedido, dejamos de usar esos datos para los fines
              que habías aceptado.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Tus derechos
            </h2>
            <p className="mt-4 text-vektor-body">
              Bajo la Ley 25.326 de Protección de Datos Personales, tenés
              derecho a acceder a tus datos, pedir que los corrijamos y
              solicitar su supresión. Para ejercer cualquiera de estos
              derechos, escribinos por el formulario de contacto. También
              podés presentar un reclamo ante la Agencia de Acceso a la
              Información Pública (AAIP), autoridad de aplicación en
              Argentina.
            </p>
          </div>

          <div>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Contacto
            </h2>
            <p className="mt-4 text-vektor-body">
              ¿Dudas sobre esta política o querés ejercer alguno de tus
              derechos? Escribinos desde nuestro formulario de contacto y te
              respondemos a la brevedad.
            </p>
          </div>
        </div>
      </section>
    </>
  );
}
