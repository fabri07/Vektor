"use client";

import { useRef, useEffect, useState, useCallback, type KeyboardEvent } from "react";
import { useRouter } from "next/navigation";
import { Send, Plus } from "lucide-react";
import { useChat } from "./hooks/useChat";
import { ApprovalCard } from "./components/ApprovalCard";
import {
  AttachmentPicker,
  type AttachmentFile,
} from "./components/AttachmentPicker";
import { onboardingService } from "@/services/onboarding.service";

const INITIAL_EXAMPLES = [
  "¡Hola! Soy tu asistente de Véktor. Podés decirme cosas como:",
  "• Hoy vendí 85 mil",
  "• Pagué alquiler 450 mil",
  "• ¿Cómo está mi negocio?",
  "• Llegó mail de mi proveedor",
];

function buildAttachmentMessage(attachments: AttachmentFile[]): string {
  if (attachments.length === 1) {
    return `Adjunté 1 archivo: ${attachments[0]?.file.name ?? "archivo"}`;
  }
  return `Adjunté ${attachments.length} archivos.`;
}

export function ChatPage() {
  const router = useRouter();
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<AttachmentFile[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const onboardingChecked = useRef(false);

  const { messages, isLoading, send, confirm, cancel, messagesUsedToday, isRateLimited, newConversation } =
    useChat();

  // Redirigir a onboarding si el negocio aún no fue configurado
  useEffect(() => {
    if (onboardingChecked.current) return;
    onboardingChecked.current = true;
    onboardingService.getStatus().then((status) => {
      if (!status.completed) {
        router.replace("/onboarding");
      }
    }).catch(() => { /* no bloquear el chat si el check falla */ });
  }, [router]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const isUploading = attachments.some((a) => a.uploading);

  const handleSend = async () => {
    const text = input.trim();
    const readyAttachments = attachments
      .filter((a) => a.uploadedFileId)
      .map((a) => ({ file_id: a.uploadedFileId!, filename: a.file.name }));
    const hasText = text.length > 0;
    const hasAttachments = readyAttachments.length > 0;
    if ((!hasText && !hasAttachments) || isLoading || isRateLimited || isUploading) {
      return;
    }

    const payloadMessage = hasText
      ? text
      : `Analizá los archivos adjuntos: ${readyAttachments.map((a) => a.filename).join(", ")}`;
    const displayMessage = hasText ? text : buildAttachmentMessage(attachments);

    setInput("");
    setAttachments([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
    await send(payloadMessage, readyAttachments, displayMessage);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSend();
    }
  };

  const handleTextareaChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setInput(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    const lineHeight = 24;
    const maxLines = 3;
    el.style.height = Math.min(el.scrollHeight, lineHeight * maxLines) + "px";
  };

  const addAttachment = useCallback((attachment: AttachmentFile) => {
    setAttachments((prev) => [...prev, attachment]);
  }, []);

  const updateAttachment = useCallback(
    (id: string, patch: Partial<AttachmentFile>) => {
      setAttachments((prev) =>
        prev.map((a) => (a.id === id ? { ...a, ...patch } : a)),
      );
    },
    [],
  );

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="border-b border-vk-border-w px-6 py-3">
        <div className="mx-auto max-w-[780px]">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-base font-semibold text-vk-text-primary">
                Asistente Véktor
              </h1>
              <p className="text-xs text-vk-text-muted">
                {messagesUsedToday}/50 mensajes disponibles hoy
              </p>
            </div>
            <button
              onClick={newConversation}
              className="flex items-center gap-1.5 rounded-lg border border-vk-border-w bg-vk-surface-w px-3 py-1.5 text-xs font-medium text-vk-text-secondary hover:bg-vk-bg-light transition-colors"
              title="Nueva conversación"
            >
              <Plus className="h-3.5 w-3.5" />
              Nueva
            </button>
          </div>
        </div>
      </div>

      {/* Messages area */}
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        <div className="mx-auto max-w-[780px] px-4 py-6 space-y-4">
          {messages.length === 0 ? (
            <div className="space-y-1 pt-4">
              {INITIAL_EXAMPLES.map((line, i) => (
                <p key={i} className="text-sm text-vk-text-muted">
                  {line}
                </p>
              ))}
            </div>
          ) : (
            messages.map((msg) => (
              <div
                key={msg.id}
                className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
              >
                {msg.status === "requires_approval" && msg.pendingActionId ? (
                  <div className="w-full max-w-[90%]">
                    <ApprovalCard
                      summary={msg.content}
                      pendingActionId={msg.pendingActionId}
                      onConfirm={confirm}
                      onCancel={cancel}
                    />
                  </div>
                ) : (
                  <div
                    className={`max-w-[85%] rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap
                      ${
                        msg.role === "user"
                          ? "bg-vk-blue text-white rounded-br-sm"
                          : msg.role === "system"
                            ? "bg-vk-danger/10 text-vk-danger text-xs"
                            : msg.status === "requires_clarification"
                              ? "bg-vk-bg-light text-vk-text-primary rounded-bl-sm ring-1 ring-vk-blue/20"
                              : "bg-vk-bg-light text-vk-text-primary rounded-bl-sm"
                      }`}
                  >
                    {msg.content}
                  </div>
                )}
              </div>
            ))
          )}

          {isLoading && (
            <div className="flex justify-start">
              <div className="bg-vk-bg-light rounded-2xl rounded-bl-sm px-4 py-2.5">
                <span className="flex gap-1 items-center text-vk-text-muted text-xs">
                  <span className="animate-bounce">•</span>
                  <span className="animate-bounce [animation-delay:0.15s]">•</span>
                  <span className="animate-bounce [animation-delay:0.3s]">•</span>
                </span>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Input — sticky bottom */}
      <div className="border-t border-vk-border-w bg-vk-surface-w p-4">
        <div className="mx-auto max-w-[780px]">
          {isRateLimited ? (
            <p className="text-center text-xs text-vk-text-muted py-2">
              Límite diario alcanzado. Disponible mañana.
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              <AttachmentPicker
                attachments={attachments}
                onAdd={addAttachment}
                onUpdate={updateAttachment}
                onRemove={removeAttachment}
                disabled={isLoading}
              />
              <div className="flex items-end gap-2">
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={handleTextareaChange}
                  onKeyDown={handleKeyDown}
                  placeholder="Escribí tu mensaje..."
                  rows={1}
                  disabled={isLoading || isUploading}
                  className="flex-1 resize-none rounded-xl border border-vk-border-w bg-vk-surface-w px-4 py-2.5 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:ring-vk-blue disabled:opacity-50"
                  style={{ minHeight: "44px", maxHeight: "72px" }}
                />
                <button
                  onClick={() => void handleSend()}
                  disabled={
                    isLoading ||
                    isUploading ||
                    (!input.trim() && attachments.length === 0)
                  }
                  className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-vk-blue text-white hover:bg-vk-blue-hover disabled:opacity-40 transition-colors"
                  aria-label="Enviar"
                >
                  <Send className="h-4 w-4" />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
