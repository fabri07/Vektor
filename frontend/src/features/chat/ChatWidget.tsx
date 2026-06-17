"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useRouter, usePathname } from "next/navigation";
import { History, Plus, Send, X } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { VektorLogo } from "@/components/ui/VektorLogo";
import { ManualEntryLauncher } from "@/features/ingestion/ManualEntryLauncher";
import { useChat } from "./hooks/useChat";
import { ApprovalCard } from "./components/ApprovalCard";
import { PendingActionGroupCard } from "./components/PendingActionGroupCard";
import { PendingActionGroupStatus } from "./components/PendingActionGroupStatus";
import { AttachmentPicker, type AttachmentFile } from "./components/AttachmentPicker";
import { ConversationList } from "./components/ConversationList";
import { getConversation } from "@/services/agent.service";
import { filesService } from "@/services/files.service";
import { onboardingService } from "@/services/onboarding.service";
import { useChatStore } from "@/stores/chatStore";
import { useChatWidgetStore } from "@/stores/chatWidgetStore";

const SUGGESTIONS = [
  "Hoy vendí $85.000",
  "Pagué alquiler $450.000",
  "¿Cómo está mi negocio?",
  "Llegó mail de mi proveedor",
];

const BTN_SIZE = 52; // px
const DRAG_THRESHOLD = 6; // px para distinguir click de arrastre

function buildAttachmentMessage(attachments: AttachmentFile[]): string {
  if (attachments.length === 1) {
    return `Adjunté 1 archivo: ${attachments[0]?.file.name ?? "archivo"}`;
  }
  return `Adjunté ${attachments.length} archivos.`;
}

export function ChatWidget() {
  const router = useRouter();
  const pathname = usePathname();
  const queryClient = useQueryClient();
  const { isOpen, open, close, posX, posY, setPosition } = useChatWidgetStore();

  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<AttachmentFile[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const onboardingChecked = useRef(false);

  // ── Arrastre del ícono ───────────────────────────────────────────────────
  const dragState = useRef<{ startX: number; startY: number; moved: boolean } | null>(null);
  const [dragPos, setDragPos] = useState<{ x: number; y: number } | null>(null);

  const {
    messages,
    isLoading,
    send,
    confirm,
    cancel,
    automate,
    dismissAutomation,
    confirmGroup,
    cancelGroup,
    messagesUsedToday,
    isRateLimited,
    newConversation,
  } = useChat();
  const { conversationId, loadMessages } = useChatStore();

  // Redirigir a onboarding si el negocio aún no fue configurado (una sola vez).
  useEffect(() => {
    if (onboardingChecked.current) return;
    onboardingChecked.current = true;
    if (pathname.startsWith("/onboarding")) return;
    onboardingService
      .getStatus()
      .then((status) => {
        if (!status.completed) router.replace("/onboarding");
      })
      .catch(() => {
        /* no bloquear si el check falla */
      });
  }, [router, pathname]);

  useEffect(() => {
    if (isOpen) messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, isOpen]);

  const isUploading = attachments.some((a) => a.uploading);

  const handleSend = async () => {
    const text = input.trim();
    const readyAttachments = attachments
      .filter((a) => a.uploadedFileId)
      .map((a) => ({ file_id: a.uploadedFileId!, filename: a.file.name }));
    const hasText = text.length > 0;
    const hasAttachments = readyAttachments.length > 0;
    if ((!hasText && !hasAttachments) || isLoading || isRateLimited || isUploading) return;

    const payloadMessage = hasText
      ? text
      : `Analizá los archivos adjuntos: ${readyAttachments.map((a) => a.filename).join(", ")}`;
    const displayMessage = hasText ? text : buildAttachmentMessage(attachments);

    setInput("");
    setAttachments([]);
    if (textareaRef.current) textareaRef.current.style.height = "auto";
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
    el.style.height = Math.min(el.scrollHeight, 72) + "px";
  };

  const addAttachment = useCallback((attachment: AttachmentFile) => {
    setAttachments((prev) => [...prev, attachment]);
  }, []);
  const updateAttachment = useCallback((id: string, patch: Partial<AttachmentFile>) => {
    setAttachments((prev) => prev.map((a) => (a.id === id ? { ...a, ...patch } : a)));
  }, []);
  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);
  const handleRetry = useCallback(
    async (id: string) => {
      const attachment = attachments.find((a) => a.id === id);
      if (!attachment) return;
      updateAttachment(id, { uploading: true, error: undefined });
      try {
        const uploaded = await filesService.upload(attachment.file, "chat");
        updateAttachment(id, { uploading: false, uploadedFileId: uploaded.id });
      } catch {
        updateAttachment(id, { uploading: false, error: "Error al subir" });
      }
    },
    [attachments, updateAttachment],
  );

  const handleSelectConversation = useCallback(
    async (selectedId: string) => {
      try {
        const data = await getConversation(selectedId);
        const msgs = data.turns.map((turn) => ({
          id: crypto.randomUUID(),
          role: turn.role as "user" | "assistant" | "system",
          content: turn.content,
          timestamp: new Date().toISOString(),
        }));
        loadMessages(selectedId, msgs);
        setShowHistory(false);
        void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      } catch {
        /* silencioso */
      }
    },
    [loadMessages, queryClient],
  );

  const handleNewConversation = useCallback(() => {
    newConversation();
    setShowHistory(false);
    void queryClient.invalidateQueries({ queryKey: ["conversations"] });
  }, [newConversation, queryClient]);

  // ── Drag handlers del ícono ───────────────────────────────────────────────
  const onPointerDown = (e: ReactPointerEvent<HTMLButtonElement>) => {
    dragState.current = { startX: e.clientX, startY: e.clientY, moved: false };
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLButtonElement>) => {
    const st = dragState.current;
    if (!st) return;
    const dx = e.clientX - st.startX;
    const dy = e.clientY - st.startY;
    if (!st.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    st.moved = true;
    const maxX = window.innerWidth - BTN_SIZE - 4;
    const maxY = window.innerHeight - BTN_SIZE - 4;
    const x = Math.min(Math.max(e.clientX - BTN_SIZE / 2, 4), maxX);
    const y = Math.min(Math.max(e.clientY - BTN_SIZE / 2, 4), maxY);
    setDragPos({ x, y });
  };

  const onPointerUp = () => {
    const st = dragState.current;
    dragState.current = null;
    if (!st) return;
    if (st.moved && dragPos) {
      setPosition(dragPos.x, dragPos.y);
    } else {
      open();
    }
  };

  // Posición del ícono: drag temporal > guardada > default abajo-derecha.
  const live = dragPos ?? (posX != null && posY != null ? { x: posX, y: posY } : null);
  const buttonStyle: React.CSSProperties = live
    ? { left: live.x, top: live.y, right: "auto", bottom: "auto" }
    : { right: 24, bottom: 24 };

  const isEmpty = messages.length === 0;

  // No mostrar el widget durante el onboarding.
  if (pathname.startsWith("/onboarding")) return null;

  return (
    <>
      {/* Ícono flotante (oculto mientras el panel está abierto) */}
      {!isOpen && (
        <button
          type="button"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          aria-label="Abrir asistente Véktor"
          style={{ position: "fixed", width: BTN_SIZE, height: BTN_SIZE, ...buttonStyle, touchAction: "none" }}
          className="z-[60] flex items-center justify-center rounded-full bg-gradient-to-br from-vektor-blue to-vektor-teal shadow-glow transition-transform hover:scale-105 active:scale-95"
        >
          <VektorLogo variant="icon" size="sm" theme="dark" />
        </button>
      )}

      {/* Panel */}
      {isOpen && (
        <div className="fixed bottom-4 right-4 z-[60] flex h-[70vh] max-h-[640px] w-[min(380px,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-vektor-border bg-vk-surface-w shadow-vk-lg">
          {/* Header */}
          <div className="flex items-center justify-between border-b border-vk-border-w px-3 py-2.5">
            <div className="flex items-center gap-2">
              <span className="flex h-7 w-7 items-center justify-center rounded-full bg-gradient-to-br from-vektor-blue to-vektor-teal">
                <VektorLogo variant="icon" size="sm" theme="dark" />
              </span>
              <div>
                <h2 className="text-sm font-semibold text-vk-text-primary">Asistente Véktor</h2>
                <p className="text-[10px] text-vk-text-muted">{messagesUsedToday}/50 mensajes hoy</p>
              </div>
            </div>
            <div className="flex items-center gap-0.5">
              <button
                type="button"
                onClick={() => setShowHistory((v) => !v)}
                aria-label="Historial de conversaciones"
                className={`rounded-md p-1.5 transition-colors ${showHistory ? "bg-vk-blue/10 text-vk-blue" : "text-vk-text-muted hover:text-vk-text-secondary"}`}
              >
                <History className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={handleNewConversation}
                aria-label="Nueva conversación"
                className="rounded-md p-1.5 text-vk-text-muted hover:text-vk-text-secondary transition-colors"
              >
                <Plus className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={close}
                aria-label="Cerrar"
                className="rounded-md p-1.5 text-vk-text-muted hover:text-vk-text-secondary transition-colors"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          {/* Carga manual (fijo en la sección de chat) */}
          <div className="border-b border-vk-border-w px-3 py-2">
            <ManualEntryLauncher variant="secondary" />
          </div>

          {/* Historial (desplegable) */}
          {showHistory ? (
            <div className="flex-1 overflow-y-auto">
              <ConversationList
                activeConversationId={conversationId}
                onSelect={(id) => void handleSelectConversation(id)}
                onNew={handleNewConversation}
              />
            </div>
          ) : (
            <>
              {/* Mensajes */}
              <div className="flex-1 overflow-y-auto scrollbar-thin px-3 py-3 space-y-3">
                {isEmpty ? (
                  <div className="space-y-3">
                    <p className="text-sm text-vk-text-secondary">
                      ¿En qué te ayudo? Probá con:
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                      {SUGGESTIONS.map((s, i) => (
                        <button
                          key={i}
                          type="button"
                          onClick={() => setInput(s)}
                          className="rounded-lg border border-vk-border-w bg-vk-surface-w px-2.5 py-1.5 text-xs text-vk-text-secondary hover:bg-vk-bg-light transition-colors"
                        >
                          {s}
                        </button>
                      ))}
                    </div>
                  </div>
                ) : (
                  messages.map((msg) => (
                    <div
                      key={msg.id}
                      className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
                    >
                      {msg.groupResult ? (
                        <div className="w-full max-w-[92%]">
                          <div className="mb-2 whitespace-pre-wrap text-sm text-vk-text-primary">
                            {msg.content}
                          </div>
                          <PendingActionGroupStatus
                            groupExecutionStatus={msg.groupResult.groupExecutionStatus}
                            tasks={msg.groupResult.tasks}
                          />
                        </div>
                      ) : msg.status === "requires_approval" && msg.approvalGroupId ? (
                        <div className="w-full max-w-[92%]">
                          <PendingActionGroupCard
                            summary={msg.content}
                            approvalGroupId={msg.approvalGroupId}
                            onConfirmGroup={confirmGroup}
                            onCancelGroup={cancelGroup}
                          />
                        </div>
                      ) : msg.status === "requires_approval" && msg.pendingActionId ? (
                        <div className="w-full max-w-[92%]">
                          <ApprovalCard
                            summary={msg.content}
                            pendingActionId={msg.pendingActionId}
                            automationOffer={msg.automationOffer}
                            onConfirm={confirm}
                            onCancel={cancel}
                            onAutomate={automate}
                            onDismissAutomation={dismissAutomation}
                          />
                        </div>
                      ) : (
                        <div
                          className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap
                            ${
                              msg.role === "user"
                                ? "bg-vk-blue text-white rounded-br-sm"
                                : msg.role === "system"
                                  ? "bg-vk-danger/10 text-vk-danger text-xs"
                                  : "bg-vk-bg-light text-vk-text-primary rounded-bl-sm"
                            }`}
                        >
                          {msg.content}
                          {msg.requiresGoogleAuth && (
                            <button
                              type="button"
                              onClick={() =>
                                msg.authUrl
                                  ? window.open(msg.authUrl, "_blank", "noopener,noreferrer")
                                  : router.push("/apps")
                              }
                              className="mt-2 block w-full rounded-lg bg-blue-600 px-3 py-1.5 text-center text-xs font-medium text-white hover:bg-blue-700 transition-colors"
                            >
                              Conectar Google
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  ))
                )}

                {isLoading && (
                  <div className="flex justify-start">
                    <div className="bg-vk-bg-light rounded-2xl rounded-bl-sm px-3 py-2">
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

              {/* Input */}
              <div className="border-t border-vk-border-w p-3">
                {isRateLimited ? (
                  <p className="py-2 text-center text-xs text-vk-text-muted">
                    Límite diario alcanzado. Disponible mañana.
                  </p>
                ) : (
                  <div className="flex flex-col gap-2">
                    <AttachmentPicker
                      attachments={attachments}
                      onAdd={addAttachment}
                      onUpdate={updateAttachment}
                      onRemove={removeAttachment}
                      onRetry={(id) => void handleRetry(id)}
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
                        className="flex-1 resize-none rounded-xl border border-vk-border-w bg-vk-surface-w px-3 py-2 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:ring-vk-blue disabled:opacity-50"
                        style={{ minHeight: "40px", maxHeight: "72px" }}
                      />
                      <button
                        type="button"
                        onClick={() => void handleSend()}
                        disabled={isLoading || isUploading || (!input.trim() && attachments.length === 0)}
                        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-vk-blue text-white hover:bg-vk-blue-hover disabled:opacity-40 transition-colors"
                        aria-label="Enviar"
                      >
                        <Send className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </>
  );
}
