import { redirect } from "next/navigation";

/**
 * `/register` — ruta conservada como redirect a `/solicitar-acceso`.
 *
 * Véktor cerró el registro abierto: ya no se crea una cuenta desde el sitio, se
 * manda una solicitud que el dueño revisa a mano. La ruta NO se borra a
 * propósito — hay links, bookmarks, mails viejos y CTAs que apuntan acá, y así
 * todos siguen funcionando sin tocarlos.
 *
 * Es un redirect de servidor: el visitante nunca ve un formulario de alta, ni
 * siquiera por un frame.
 */
export default function RegisterPage() {
  redirect("/solicitar-acceso");
}
