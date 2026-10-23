"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { ArrowRight, BookOpen, Loader2, MessageCircle, Send } from "lucide-react";
import { sendHelpMessage } from "@/services/agent.service";

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
  redirectTo?: "main_chat" | "help_chat";
}

const QUICK_QUESTIONS = [
  "¿Cómo cargo una venta?",
  "¿Qué es el health score?",
  "¿Cómo conecto Google?",
  "¿Cómo importo un Excel?",
  "¿Cómo cambio mi objetivo de margen?",
];

export function HelpChat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const conversationId = useRef<string>(
    `help-${Math.random().toString(36).slice(2)}`,
  );

  async function submit(text: string) {
    if (!text.trim() || loading) return;
    const userMsg: Message = {
      id: crypto.randomUUID(),
      role: "user",
      text: text.trim(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    try {
      const res = await sendHelpMessage(text.trim(), conversationId.current);
      const summary =
        (res.result?.summary as string) || res.message || "Sin respuesta.";
      const redirectTo = res.result?.redirect_to as "main_chat" | "help_chat" | undefined;
      const assistantMsg: Message = {
        id: crypto.randomUUID(),
        role: "assistant",
        text: summary,
        redirectTo,
      };
      setMessages((prev) => [...prev, assistantMsg]);
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "assistant",
          text: "Error temporal. Intentá de nuevo.",
        },
      ]);
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  }

  return (
    <div className="flex h-full flex-col">
      {/* Intro cuando no hay mensajes */}
      {messages.length === 0 && (
        <div className="flex flex-1 flex-col items-center justify-center gap-6 px-4 py-10 text-center">
          <BookOpen className="h-10 w-10 text-vk-text-muted" />
          <div>
            <p className="text-lg font-semibold text-vk-text-primary">
              Centro de ayuda de Véktor
            </p>
            <p className="mt-2 max-w-sm text-sm leading-6 text-vk-text-secondary">
              Preguntame sobre cómo usar la plataforma. Para cargar ventas,
              gastos o consultar el estado del negocio, usá el{" "}
              <Link href="/chat" className="text-vk-blue underline">
                chat principal
              </Link>
              .
            </p>
          </div>
          <div className="flex flex-wrap justify-center gap-2">
            {QUICK_QUESTIONS.map((q) => (
              <button
                key={q}
                type="button"
                onClick={() => submit(q)}
                className="rounded-full border border-vk-border-w bg-vk-surface-w px-4 py-2 text-xs text-vk-text-secondary hover:text-vk-text-primary transition-colors"
              >
                {q}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Historial de mensajes */}
      {messages.length > 0 && (
        <div className="flex-1 overflow-y-auto space-y-4 px-4 py-6">
          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-[85%] rounded-2xl px-4 py-3 text-sm leading-6 ${
                  msg.role === "user"
                    ? "bg-vk-blue text-white"
                    : "bg-vk-bg-light border border-vk-border-w text-vk-text-secondary"
                }`}
              >
                <p className="whitespace-pre-wrap">{msg.text}</p>
                {msg.redirectTo === "main_chat" && (
                  <Link
                    href="/chat"
                    className="mt-3 inline-flex items-center gap-1.5 text-xs font-medium text-vk-blue hover:underline"
                  >
                    Ir al chat principal
                    <ArrowRight className="h-3.5 w-3.5" />
                  </Link>
                )}
              </div>
            </div>
          ))}
          {loading && (
            <div className="flex justify-start">
              <div className="bg-vk-bg-light border border-vk-border-w rounded-2xl px-4 py-3">
                <Loader2 className="h-4 w-4 animate-spin text-vk-text-muted" />
              </div>
            </div>
          )}
        </div>
      )}

      {/* Input */}
      <div className="border-t border-vk-border-w p-4">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit(input);
          }}
          className="flex items-center gap-3"
        >
          <input
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="¿Cómo puedo ayudarte?"
            disabled={loading}
            className="flex-1 rounded-full border border-vk-border-w bg-vk-surface-w px-4 py-2.5 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-1 focus:ring-vk-blue disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={!input.trim() || loading}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-vk-blue text-white disabled:opacity-40"
          >
            <Send className="h-4 w-4" />
          </button>
        </form>
        <p className="mt-2 text-center text-[10px] text-vk-text-muted">
          Este chat no consume tus mensajes diarios
        </p>
      </div>
    </div>
  );
}
