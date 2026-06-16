"use client";

import { useMemo, useState } from "react";
import axios from "axios";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Mail, MessageCircle } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { Tooltip } from "@/components/ui/Tooltip";
import { formatDateTime } from "@/lib/datetime";
import { useToastStore } from "@/stores/toastStore";
import {
  communicationService,
  type ChannelAvailability,
  type CommunicationChannel,
  type CommunicationLogResponse,
  type RecipientType,
  type SendMessagePayload,
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

function sendMessage(
  recipientType: RecipientType,
  id: string,
  payload: SendMessagePayload,
): Promise<CommunicationLogResponse> {
  return recipientType === "customer"
    ? communicationService.sendToCustomer(id, payload)
    : communicationService.sendToSupplier(id, payload);
}

function fetchHistory(
  recipientType: RecipientType,
  id: string,
): Promise<CommunicationLogResponse[]> {
  return recipientType === "customer"
    ? communicationService.getCustomerHistory(id)
    : communicationService.getSupplierHistory(id);
}

function isChannelNotConfigured(err: unknown): boolean {
  if (!axios.isAxiosError(err) || err.response?.status !== 503) return false;
  const detail = (err.response.data as { detail?: { code?: string } } | undefined)?.detail;
  return detail?.code === "CHANNEL_NOT_CONFIGURED";
}

function errorMessage(err: unknown): string {
  if (isChannelNotConfigured(err)) {
    return "El canal no está configurado todavía. Probá de nuevo más tarde.";
  }
  if (axios.isAxiosError(err) && err.response?.status === 400) {
    return "No se pudo enviar: el destinatario no tiene un dato de contacto válido.";
  }
  return "No se pudo enviar el mensaje. Intentá de nuevo.";
}

export function ContactCommunication({
  recipientType,
  recipientId,
  email,
  phone,
}: ContactCommunicationProps) {
  const toast = useToastStore((s) => s.add);
  const queryClient = useQueryClient();
  const [composeChannel, setComposeChannel] = useState<CommunicationChannel | null>(null);

  const historyKey = ["comm-history", recipientType, recipientId] as const;

  const { data: channels = [] } = useQuery({
    queryKey: ["comm-channels"],
    queryFn: () => communicationService.getChannels(),
    staleTime: 5 * 60 * 1000,
  });

  const { data: history = [], isLoading: historyLoading } = useQuery({
    queryKey: historyKey,
    queryFn: () => fetchHistory(recipientType, recipientId),
    enabled: Boolean(recipientId),
    staleTime: 30 * 1000,
  });

  const channelMap = useMemo(() => {
    const map = new Map<CommunicationChannel, ChannelAvailability>();
    for (const c of channels) map.set(c.channel, c);
    return map;
  }, [channels]);

  const sendMutation = useMutation({
    mutationFn: (payload: SendMessagePayload) =>
      sendMessage(recipientType, recipientId, payload),
    onSuccess: async (log) => {
      setComposeChannel(null);
      if (log.status === "sent") {
        toast("Mensaje enviado.", "success");
      } else {
        toast(log.error ?? "El mensaje no se pudo entregar.", "error");
      }
      await queryClient.invalidateQueries({ queryKey: historyKey });
    },
    onError: (err) => toast(errorMessage(err), "error"),
  });

  const emailAvailable = channelMap.get("email")?.available ?? false;
  const whatsappAvailable = channelMap.get("whatsapp")?.available ?? false;

  const hasEmail = Boolean(email?.trim());
  const hasPhone = Boolean(phone?.trim());

  const emailEnabled = emailAvailable && hasEmail;
  const whatsappEnabled = whatsappAvailable && hasPhone;

  const emailTooltip = !emailAvailable
    ? "No disponible"
    : !hasEmail
      ? "Sin email"
      : null;
  const whatsappTooltip = !whatsappAvailable
    ? "Disponible próximamente"
    : !hasPhone
      ? "No disponible"
      : null;

  return (
    <section className="space-y-3">
      <h3 className="font-display text-sm font-semibold text-vk-text-primary">
        Comunicación
      </h3>

      <div className="flex flex-wrap items-center gap-2">
        <ChannelButton
          icon={<Mail className="h-4 w-4" />}
          label={CHANNEL_LABELS.email}
          enabled={emailEnabled}
          tooltip={emailTooltip}
          onClick={() => setComposeChannel("email")}
        />
        <ChannelButton
          icon={<MessageCircle className="h-4 w-4" />}
          label={CHANNEL_LABELS.whatsapp}
          enabled={whatsappEnabled}
          tooltip={whatsappTooltip}
          onClick={() => setComposeChannel("whatsapp")}
        />
      </div>

      {/* Historial de mensajes */}
      <div className="space-y-2">
        <p className="text-xs font-medium text-vk-text-secondary">Historial de mensajes</p>
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
            Todavía no se enviaron mensajes.
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

      <ComposeModal
        channel={composeChannel}
        sending={sendMutation.isPending}
        onClose={() => setComposeChannel(null)}
        onSend={(payload) => sendMutation.mutate(payload)}
      />
    </section>
  );
}

// ── Channel button (with disabled tooltip) ─────────────────────────────────────

function ChannelButton({
  icon,
  label,
  enabled,
  tooltip,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  enabled: boolean;
  tooltip: string | null;
  onClick: () => void;
}) {
  const button = (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      disabled={!enabled}
      onClick={onClick}
    >
      {icon}
      {label}
    </Button>
  );

  if (enabled || !tooltip) return button;

  // Tooltip needs a hoverable target; wrap disabled button in a span.
  return (
    <Tooltip content={tooltip}>
      <span className="inline-flex cursor-not-allowed">{button}</span>
    </Tooltip>
  );
}

// ── Compose modal ──────────────────────────────────────────────────────────────

function ComposeModal({
  channel,
  sending,
  onClose,
  onSend,
}: {
  channel: CommunicationChannel | null;
  sending: boolean;
  onClose: () => void;
  onSend: (payload: SendMessagePayload) => void;
}) {
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Reset fields whenever the modal target changes.
  const isOpen = channel !== null;

  function handleClose() {
    setSubject("");
    setBody("");
    setError(null);
    onClose();
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!body.trim()) {
      setError("El mensaje no puede estar vacío.");
      return;
    }
    if (!channel) return;
    const trimmedSubject = subject.trim();
    onSend({
      channel,
      body: body.trim(),
      ...(trimmedSubject ? { subject: trimmedSubject } : {}),
    });
  }

  const title =
    channel === "email"
      ? "Enviar email"
      : channel === "whatsapp"
        ? "Enviar WhatsApp"
        : "Enviar mensaje";

  return (
    <Modal isOpen={isOpen} onClose={handleClose} title={title} size="lg">
      <form className="space-y-4" onSubmit={handleSubmit}>
        {channel === "email" ? (
          <Input
            label="Asunto (opcional)"
            type="text"
            placeholder="Ej: Recordatorio de pago"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
          />
        ) : null}

        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="comm-body"
            className="text-xs font-medium text-vk-text-secondary"
          >
            Mensaje
          </label>
          <textarea
            id="comm-body"
            rows={5}
            placeholder="Escribí tu mensaje…"
            value={body}
            onChange={(e) => {
              setBody(e.target.value);
              if (error) setError(null);
            }}
            className="w-full rounded-xl border border-vk-border-w bg-vektor-surface px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder transition-all duration-200 focus:border-vk-blue/40 focus:outline-none focus:ring-2 focus:ring-vk-blue/15"
          />
          {error ? <p className="text-xs text-vk-danger">{error}</p> : null}
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" size="sm" onClick={handleClose}>
            Cancelar
          </Button>
          <Button type="submit" size="sm" loading={sending}>
            Enviar
          </Button>
        </div>
      </form>
    </Modal>
  );
}
