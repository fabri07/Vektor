"use client";

import { useRef, useEffect, useState, KeyboardEvent } from "react";
import { MessageSquare, X, Send } from "lucide-react";
import { useChat } from "./hooks/useChat";
import { ApprovalCard } from "./components/ApprovalCard";

const INITIAL_EXAMPLES = [
  "¡Hola! Soy tu asistente de Véktor. Podés decirme cosas como:",
  "• Hoy vendí 85 mil",
  "• Pagué alquiler 450 mil",
  "• ¿Cómo está mi negocio?",
  "• Llegó mail de mi proveedor",
];

export function ChatPanel() {
  const [isOpen, setIsOpen] = useState(false);
  const [hasUnread, setHasUnread] = useState(false);
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const { messages, isLoading, send, confirm, cancel, messagesUsedToday, isRateLimited } =
    useChat();

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    if (!isOpen && messages.length > 0) {
      setHasUnread(true);
    }
  }, [messages, isOpen]);

  const handleOpen = () => {
    setIsOpen(true);
    setHasUnread(false);
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text || isLoading || isRateLimited) return;
    setInput("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
    await send(text);
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

  return (
    <>
      {/* Floating button */}
      <button
        onClick={handleOpen}
        className="fixed bottom-6 right-6 z-50 flex h-14 w-14 items-center justify-center rounded-full bg-blue-600 text-white shadow-lg hover:bg-blue-700 transition-colors md:bottom-8 md:right-8"
        aria-label="Abrir asistente"
      >
        <MessageSquare className="h-6 w-6" />
        {hasUnread && (
          <span className="absolute -top-1 -right-1 h-4 w-4 rounded-full bg-red-500" />
        )}
      </button>

      {/* Panel overlay (mobile) */}
      {isOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/40 md:hidden"
          onClick={() => setIsOpen(false)}
        />
      )}

      {/* Panel */}
      <div
        className={`fixed z-50 flex flex-col bg-white shadow-2xl transition-transform duration-300
          inset-0 md:inset-auto md:right-0 md:top-0 md:h-screen md:w-[380px]
          ${isOpen ? "translate-x-0" : "translate-x-full"}`}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b px-4 py-3">
          <div>
            <h2 className="font-semibold text-gray-800">Asistente Véktor</h2>
            <p className="text-xs text-gray-400">
              {messagesUsedToday}/50 mensajes disponibles hoy
            </p>
          </div>
          <button
            onClick={() => setIsOpen(false)}
            className="rounded-md p-1 text-gray-400 hover:text-gray-600"
            aria-label="Cerrar"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {messages.length === 0 ? (
            <div className="space-y-1">
              {INITIAL_EXAMPLES.map((line, i) => (
                <p key={i} className="text-sm text-gray-500">
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
                    className={`max-w-[85%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap
                      ${msg.role === "user"
                        ? "bg-blue-600 text-white rounded-br-sm"
                        : msg.role === "system"
                        ? "bg-red-50 text-red-700 text-xs"
                        : "bg-gray-100 text-gray-800 rounded-bl-sm"
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
              <div className="bg-gray-100 rounded-2xl rounded-bl-sm px-3 py-2">
                <span className="flex gap-1 items-center text-gray-400 text-xs">
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
        <div className="border-t p-3">
          {isRateLimited ? (
            <p className="text-center text-xs text-gray-400 py-2">
              Límite diario alcanzado. Disponible mañana.
            </p>
          ) : (
            <div className="flex items-end gap-2">
              <textarea
                ref={textareaRef}
                value={input}
                onChange={handleTextareaChange}
                onKeyDown={handleKeyDown}
                placeholder="Escribí tu mensaje..."
                rows={1}
                disabled={isLoading}
                className="flex-1 resize-none rounded-xl border border-gray-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
                style={{ minHeight: "40px", maxHeight: "72px" }}
              />
              <button
                onClick={() => void handleSend()}
                disabled={isLoading || !input.trim()}
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40 transition-colors"
                aria-label="Enviar"
              >
                <Send className="h-4 w-4" />
              </button>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
