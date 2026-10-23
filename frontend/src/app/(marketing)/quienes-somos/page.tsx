import { PageHeader } from "@/components/public/PageHeader";

export const metadata = {
  title: "Por qué Véktor | Véktor",
  description:
    "Véktor es un asistente de salud financiera con IA para PYMEs argentinas. Determinístico en la plata, no inventa números, hecho en Argentina.",
};

export default function QuienesSomosPage() {
  return (
    <>
      <PageHeader
        title="Por qué Véktor"
        subtitle={
          <>
            Creamos Véktor para que gestionar un negocio no dependa de
            recordar todo ni de adivinar a fin de mes.
          </>
        }
      />

      <section className="mx-auto max-w-3xl space-y-10 px-6 pb-24">
        <div className="vektor-card p-8">
          <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
            Nuestra misión
          </h2>
          <p className="mt-4 text-vektor-body">
            Quienes sostienen una PYME toman decisiones todos los días, muchas
            veces con información dispersa entre la memoria, el cuaderno y una
            planilla. Nuestra misión es convertir ese movimiento cotidiano en
            respuestas claras: cuánto queda, qué está en riesgo y qué decisión
            conviene tomar ahora.
          </p>
        </div>

        <div className="vektor-card p-8">
          <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
            Qué es Véktor
          </h2>
          <p className="mt-4 text-vektor-body">
            Véktor es un asistente de gestión financiera que se adapta a tu
            rubro. Le enviás ventas, gastos y compras por chat o archivo;
            Véktor los ordena y te devuelve una lectura accionable de caja,
            margen, stock y proveedores.
          </p>
          <p className="mt-4 text-vektor-body">
            No reemplaza tu sistema contable ni pretende convertirse en otro
            ERP. Ocupa el espacio que suele quedar vacío: traducir los números
            del negocio en decisiones comprensibles y oportunas.
          </p>
        </div>

        <div className="vektor-card p-8">
          <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
            Cómo trabajamos
          </h2>
          <ul className="mt-4 space-y-4 text-vektor-body">
            <li>
              <span className="font-semibold text-vektor-white">
                Los cálculos no se improvisan.
              </span>{" "}
              Un motor determinístico obtiene cada cifra. La inteligencia
              artificial la interpreta y la explica, pero no la inventa.
            </li>
            <li>
              <span className="font-semibold text-vektor-white">
                La incertidumbre también se informa.
              </span>{" "}
              Si faltan datos, Véktor lo señala y pide lo necesario antes de
              concluir. Una respuesta incompleta es mejor que una certeza
              falsa.
            </li>
            <li>
              <span className="font-semibold text-vektor-white">
                Tu información sigue siendo tuya.
              </span>{" "}
              La tratamos con criterios de privacidad y conforme a la
              normativa argentina de protección de datos.
            </li>
            <li>
              <span className="font-semibold text-vektor-white">
                Hecho para el negocio argentino.
              </span>{" "}
              Véktor contempla la caja en efectivo, el fiado, la inflación y
              la operación real de una PYME local.
            </li>
          </ul>
        </div>
      </section>
    </>
  );
}
