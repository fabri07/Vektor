import Link from "next/link";
import { Store, SprayCan, Home } from "lucide-react";

export const metadata = {
  title: "Rubros | Véktor",
  description:
    "Véktor está pensado para kioscos, negocios de limpieza y decoración del hogar: caja, stock, márgenes y proveedores en un solo lugar.",
};

export default function RubrosPage() {
  return (
    <>
      <section className="mx-auto max-w-3xl px-6">
        <p className="pt-20 text-center text-xs font-semibold uppercase tracking-[0.22em] text-vektor-teal">
          Para tu rubro
        </p>
        <h1 className="mt-4 text-center font-display text-4xl font-bold uppercase leading-tight tracking-tight text-vektor-white sm:text-5xl">
          Pensado para tu negocio
        </h1>
        <p className="mx-auto mt-5 max-w-2xl text-center text-lg leading-8 text-vektor-body">
          No importa el rubro: Véktor entiende cómo entra y sale la plata, qué
          se vende y qué se te queda parado en el depósito. Elegí el tuyo y
          mirá cómo te ayuda.
        </p>
      </section>

      <section className="mx-auto max-w-4xl space-y-8 px-6 py-16">
        <div
          id="kiosco"
          className="vektor-card scroll-mt-24 p-8"
        >
          <div className="mb-4 flex items-center gap-3">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal">
              <Store className="h-5 w-5 text-white" />
            </span>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Kiosco / Almacén
            </h2>
          </div>
          <p className="text-vektor-body">
            En el kiosco todo pasa por la caja y por la mercadería. Véktor te
            ordena las ventas del día, te avisa cuándo se te está por acabar un
            producto y te muestra qué margen real te deja cada cosa que vendés.
          </p>
          <p className="mt-3 text-vektor-body">
            Cargás una compra al proveedor y el stock se actualiza solo; vendés
            y el inventario baja. Sin planillas, sin adivinar.
          </p>
          <ul className="mt-6 space-y-2 text-vektor-muted">
            <li>• Control de caja diaria y cierre de efectivo.</li>
            <li>• Alertas de quiebre de stock antes de quedarte sin nada.</li>
            <li>• Margen por producto para saber qué te conviene reponer.</li>
          </ul>
        </div>

        <div
          id="limpieza"
          className="vektor-card scroll-mt-24 p-8"
        >
          <div className="mb-4 flex items-center gap-3">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal">
              <SprayCan className="h-5 w-5 text-white" />
            </span>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Limpieza
            </h2>
          </div>
          <p className="text-vektor-body">
            En limpieza manejás insumos a granel, envases y pedidos que a veces
            son a cuenta. Véktor separa lo que es costo de mercadería de lo que
            es gasto fijo, y te muestra qué clientes te deben y cuánto.
          </p>
          <p className="mt-3 text-vektor-body">
            Así sabés si estás ganando plata de verdad o si el fiado te está
            comiendo la caja.
          </p>
          <ul className="mt-6 space-y-2 text-vektor-muted">
            <li>• Seguimiento de cuentas por cobrar y fiado.</li>
            <li>• Costos de insumos separados de los gastos fijos.</li>
            <li>• Márgenes por línea de producto o servicio.</li>
          </ul>
        </div>

        <div
          id="deco"
          className="vektor-card scroll-mt-24 p-8"
        >
          <div className="mb-4 flex items-center gap-3">
            <span className="flex h-11 w-11 items-center justify-center rounded-full bg-gradient-to-r from-vektor-blue to-vektor-teal">
              <Home className="h-5 w-5 text-white" />
            </span>
            <h2 className="font-display text-2xl font-bold uppercase tracking-tight text-vektor-white">
              Decoración del hogar
            </h2>
          </div>
          <p className="text-vektor-body">
            En decoración tenés muchos productos distintos, precios que se mueven
            y stock que a veces rota lento. Véktor te muestra qué se vende bien,
            qué tenés parado ocupando plata y cómo vienen tus proveedores.
          </p>
          <p className="mt-3 text-vektor-body">
            Con eso decidís qué reponer, qué liquidar y a quién comprarle mejor.
          </p>
          <ul className="mt-6 space-y-2 text-vektor-muted">
            <li>• Detección de sobrestock y productos de baja rotación.</li>
            <li>• Análisis de proveedores y precios de compra.</li>
            <li>• Márgenes para ajustar precios sin perder plata.</li>
          </ul>
        </div>
      </section>

      <section className="mx-auto max-w-3xl px-6 pb-24 text-center">
        <p className="text-vektor-body">
          ¿Tu negocio es de otro rubro? Igual te puede servir.
        </p>
        <Link
          href="/register"
          className="mt-6 inline-flex items-center justify-center rounded-full bg-white px-7 py-3 text-sm font-semibold text-vektor-night transition hover:bg-white/90"
        >
          Empezar gratis
        </Link>
      </section>
    </>
  );
}
