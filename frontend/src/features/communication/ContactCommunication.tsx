"use client";

import { useQuery } from "@tanstack/react-query";
import { ExternalLink, Mail, MessageCircle } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { Tooltip } from "@/components/ui/Tooltip";
import { formatDateTime } from "@/lib/datetime";
import { isLikelyValidWhatsApp, whatsappDigits } from "@/lib/fiscal";
import {
  communicationService,
  type CommunicationChannel,
  type CommunicationLogResponse,
  type RecipientType,
} from "@/services/communication.service";

interface ContactCommunicationProps {
  recipientType: RecipientType;
  recipientId: string;
  email: string | null;
  phone: string | null;
}

const CHANNEL_LABELS: Record<CommunicationChannel, string> = {
  email: "Email",
  whatsapp: "WhatsApp",
};

function fetchHistory(
  recipientType: RecipientType,
  id: string,
): Promise<CommunicationLogResponse[]> {
  return recipientType === "customer"
    ? communicationService.getCustomerHistory(id)
    : communicationService.getSupplierHistory(id);
}

// Compose de Gmail web, con el destinatario precargado. El usuario escribe el
// mensaje en Gmail — Véktor no envía ni registra estos mensajes externos.
function gmailComposeUrl(email: string): string {
  return `https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(email)}`;
}

// Clases del botón secundario (espejo de components/ui/Button), reusadas para
// renderizar enlaces externos con la misma apariencia.
const LINK_BASE =
  "inline-flex h-8 items-center justify-center gap-1.5 rounded-xl border px-3 text-xs font-medium transition-all duration-200 focus-visible:outline-none focus-visible:ring-2";
const LINK_ENABLED =
  "border-vektor-border bg-vektor-surface text-vektor-white hover:border-vektor-blue/40 hover:bg-vektor-surface/90 focus-visible:ring-vektor-blue/30";
const LINK_DISABLED =
  "cursor-not-allowed border-vektor-border bg-vektor-surface text-vektor-white opacity-40";

export function ContactCommunication({
  recipientType,
  recipientId,
  email,
  phone,
}: ContactCommunicationProps) {
  const historyKey = ["comm-history", recipientType, recipientId] as const;

  const { data: history = [], isLoading: historyLoading } = useQuery({
    queryKey: historyKey,
    queryFn: () => fetchHistory(recipientType, recipientId),
    enabled: Boolean(recipientId),
    staleTime: 30 * 1000,
  });

  const emailValue = email?.trim() ?? "";
  const waDigits = whatsappDigits(phone ?? "");
  const phoneValid = isLikelyValidWhatsApp(phone);

  const emailTooltip = emailValue ? null : "Sin email";
  const whatsappTooltip = !phone?.trim()
    ? "Sin teléfono"
    : !phoneValid
      ? "Revisá el número: falta código de país/área"
      : null;

  return (
    <section className="space-y-3">
      <h3 className="font-display text-sm font-semibold text-vk-text-primary">
        Comunicación
      </h3>

      <div className="flex flex-wrap items-center gap-2">
        <ContactLink
          icon={<Mail className="h-4 w-4" />}
          label={CHANNEL_LABELS.email}
          href={emailValue ? gmailComposeUrl(emailValue) : null}
          tooltip={emailTooltip}
        />
        <ContactLink
          icon={<MessageCircle className="h-4 w-4" />}
          label={CHANNEL_LABELS.whatsapp}
          href={phoneValid ? `https://wa.me/${waDigits}` : null}
          tooltip={whatsappTooltip}
        />
      </div>

      {/* Historial: solo mensajes enviados desde Véktor. Los abiertos en apps
          externas (Gmail/WhatsApp) NO quedan registrados acá. */}
      <div className="space-y-2">
        <p className="text-xs font-medium text-vk-text-secondary">
          Mensajes enviados anteriormente desde Véktor
        </p>
        {historyLoading ? (
          <div className="space-y-2">
            {[...Array<number>(2)].map((_, i) => (
              <div
                key={i}
                className="h-9 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
              />
            ))}
          </div>
        ) : history.length === 0 ? (
          <p className="rounded-lg border border-vk-border-w bg-vk-surface-w px-4 py-3 text-sm text-vk-text-muted">
            Todavía no se enviaron mensajes desde Véktor.
          </p>
        ) : (
          <div className="overflow-hidden rounded-lg border border-vk-border-w">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-vk-border-w bg-vk-bg-light text-left">
                  <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Fecha</th>
                  <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Canal</th>
                  <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Asunto</th>
                  <th className="px-3 py-2 text-xs font-semibold text-vk-text-secondary">Estado</th>
                </tr>
              </thead>
              <tbody>
                {history.map((log) => (
                  <tr key={log.id} className="border-b border-vk-border-w last:border-b-0">
                    <td className="px-3 py-2 text-vk-text-primary">
                      {formatDateTime(log.created_at)}
                    </td>
                    <td className="px-3 py-2 text-vk-text-secondary">
                      {CHANNEL_LABELS[log.channel]}
                    </td>
                    <td className="px-3 py-2 text-vk-text-secondary">
                      {log.subject?.trim() || "—"}
                    </td>
                    <td className="px-3 py-2">
                      {log.status === "sent" ? (
                        <Badge variant="success">Enviado</Badge>
                      ) : (
                        <Badge variant="danger">Falló</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

// ── Enlace externo (con tooltip cuando está deshabilitado) ──────────────────────

function ContactLink({
  icon,
  label,
  href,
  tooltip,
}: {
  icon: React.ReactNode;
  label: string;
  href: string | null;
  tooltip: string | null;
}) {
  if (href) {
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className={`${LINK_BASE} ${LINK_ENABLED}`}
      >
        {icon}
        {label}
        <ExternalLink className="h-3.5 w-3.5" />
      </a>
    );
  }

  const disabled = (
    <span className={`${LINK_BASE} ${LINK_DISABLED}`} aria-disabled="true">
      {icon}
      {label}
    </span>
  );

  if (!tooltip) return disabled;
  return (
    <Tooltip content={tooltip}>
      <span className="inline-flex">{disabled}</span>
    </Tooltip>
  );
}
