interface SubscriptionLockedNoticeProps {
  /** `compact` para un panel dentro de una pantalla; `full` para la pantalla entera. */
  variant?: "full" | "compact";
}

/**
 * Lo que se ve en lugar del análisis cuando la suscripción está bloqueada
 * (402 `SUBSCRIPTION_INSIGHTS_LOCKED`). Dice qué se pausó y, sobre todo, qué
 * NO: los datos siguen guardados y la caja sigue cobrando. Un error genérico
 * acá hacía pensar que se había perdido algo.
 */
export function SubscriptionLockedNotice({ variant = "full" }: SubscriptionLockedNoticeProps) {
  const compacto = variant === "compact";
  return (
    <div
      role="status"
      data-testid="analisis-bloqueado"
      className={`rounded-xl border border-vk-warning/40 bg-vk-warning-bg ${
        compacto ? "p-3 text-sm" : "p-5"
      }`}
    >
      <p className="font-semibold text-vk-warning">El análisis está pausado</p>
      <p className="mt-1 text-vk-text-secondary">
        {compacto
          ? "La suscripción del negocio está en solo lectura."
          : "La suscripción del negocio está en solo lectura. Tus datos siguen guardados, podés verlos y corregirlos, y la caja sigue cobrando. Para volver a ver el score, los insights y las proyecciones, regularizá la suscripción con Véktor."}
      </p>
    </div>
  );
}
