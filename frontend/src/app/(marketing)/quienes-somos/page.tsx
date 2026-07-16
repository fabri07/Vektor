import { PageHeader } from "@/components/public/PageHeader";

export const metadata = {
  title: "Quiénes somos | Véktor",
  description:
    "Véktor es un asistente de salud financiera con IA para PYMEs argentinas. Determinístico en la plata, no inventa números, hecho en Argentina.",
};

export default function QuienesSomosPage() {
  return (
    <>
      <PageHeader
        title="Quiénes somos"
        subtitle={
          <>
            Ayudamos a que un dueño de PYME decida con datos, sin vivir
            atrapado en planillas.
          </>
        }
      />

      <section className="mx-auto max-w-3xl space-y-10 px-6 pb-24">
        <div className="vektor-card p-8">
          <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
            Nuestra misión
          </h2>
          <p className="mt-4 text-vektor-body">
            La mayoría de los dueños de kioscos, negocios de limpieza y
            decoración llevan las cuentas de memoria o en una planilla que solo
            ellos entienden. Al final del mes no saben con certeza si ganaron
            plata. Véktor existe para cambiar eso: queremos que cualquier PYME
            pueda ver la salud real de su negocio y tomar decisiones con datos,
            sin necesidad de ser contador ni pasar horas cargando números.
          </p>
        </div>

        <div className="vektor-card p-8">
          <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
            Qué es Véktor
          </h2>
          <p className="mt-4 text-vektor-body">
            Véktor es un asistente de salud financiera con inteligencia
            artificial que entiende cómo funciona tu negocio. Cargás tus ventas,
            gastos y compras —escribiéndole al chat o subiendo un archivo— y
            Véktor te devuelve un panorama claro: cuánta plata tenés, qué margen
            te deja cada producto, cómo viene el stock y dónde estás perdiendo.
          </p>
          <p className="mt-4 text-vektor-body">
            No es un ERP ni un sistema contable: es un compañero que traduce tus
            números a decisiones concretas, en tu idioma.
          </p>
        </div>

        <div className="vektor-card p-8">
          <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
            Cómo trabajamos
          </h2>
          <ul className="mt-4 space-y-4 text-vektor-body">
            <li>
              <span className="font-semibold text-vektor-white">
                Determinísticos con la plata.
              </span>{" "}
              Los números los calcula un motor determinístico, no la IA. La
              inteligencia artificial explica y aconseja, pero nunca inventa una
              cifra.
            </li>
            <li>
              <span className="font-semibold text-vektor-white">
                No inventamos datos.
              </span>{" "}
              Si faltan datos para una conclusión, te lo decimos y te pedimos lo
              que falta. Preferimos ser honestos antes que mostrar un número que
              suene lindo pero sea falso.
            </li>
            <li>
              <span className="font-semibold text-vektor-white">
                Cuidamos tu información.
              </span>{" "}
              Tus datos son tuyos. Los tratamos con foco en la privacidad y el
              cumplimiento de la normativa argentina de protección de datos.
            </li>
            <li>
              <span className="font-semibold text-vektor-white">
                Hecho en Argentina.
              </span>{" "}
              Pensado para la realidad de las PYMEs de acá: el fiado, la caja en
              efectivo, la inflación y la forma de laburar del comerciante
              argentino.
            </li>
          </ul>
        </div>
      </section>
    </>
  );
}
