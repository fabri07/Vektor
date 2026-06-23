"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Search } from "lucide-react";
import { getHelpFaqs } from "@/services/agent.service";

function FaqItem({ q, a }: { q: string; a: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b border-vk-border-w last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-3 py-3 text-left text-sm font-medium text-vk-text-primary transition-colors hover:text-vk-blue"
      >
        {q}
        <ChevronDown
          className={`h-4 w-4 shrink-0 text-vk-text-muted transition-transform duration-200 ${
            open ? "rotate-180" : ""
          }`}
        />
      </button>
      {open && <p className="pb-3 text-sm leading-relaxed text-vk-text-secondary">{a}</p>}
    </div>
  );
}

/** Centro de ayuda navegable: preguntas frecuentes agrupadas por sección de Véktor,
 * con buscador. Lee el manual del backend (no usa LLM). */
export function HelpCenter() {
  const { data: sections = [], isLoading, isError } = useQuery({
    queryKey: ["help-faqs"],
    queryFn: getHelpFaqs,
    staleTime: 30 * 60 * 1000,
  });

  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sections;
    return sections
      .map((s) => ({
        ...s,
        faqs: s.faqs.filter(
          (f) => f.q.toLowerCase().includes(q) || f.a.toLowerCase().includes(q),
        ),
      }))
      .filter((s) => s.faqs.length > 0);
  }, [sections, query]);

  return (
    <div className="rounded-xl border border-vk-border-w bg-vk-surface-w p-6">
      <h2 className="text-base font-semibold text-vk-text-primary">
        Preguntas frecuentes por sección
      </h2>
      <p className="mb-4 mt-1 text-sm text-vk-text-muted">
        Buscá tu duda o explorá por sección. Si no la encontrás, preguntale al asistente
        más abajo.
      </p>

      <div className="relative mb-5">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-vk-text-muted" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Buscar una pregunta… (ej: cargar archivo, columna, score)"
          className="w-full rounded-lg border border-vk-border-w bg-vk-surface-w py-2 pl-9 pr-3 text-sm text-vk-text-primary placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:ring-vk-blue/20"
        />
      </div>

      {isLoading && <p className="text-sm text-vk-text-muted">Cargando centro de ayuda…</p>}
      {isError && (
        <p className="text-sm text-vk-danger">No se pudo cargar el centro de ayuda.</p>
      )}
      {!isLoading && !isError && filtered.length === 0 && (
        <p className="text-sm text-vk-text-muted">
          No encontramos preguntas para “{query}”. Probá con otras palabras o usá el
          asistente.
        </p>
      )}

      <div className="space-y-6">
        {filtered.map((section) => (
          <section key={section.slug}>
            <h3 className="text-sm font-semibold uppercase tracking-wide text-vk-blue">
              {section.name}
            </h3>
            {section.description && (
              <p className="mb-2 mt-0.5 text-xs text-vk-text-muted">{section.description}</p>
            )}
            <div className="rounded-lg border border-vk-border-w px-4">
              {section.faqs.map((f, i) => (
                <FaqItem key={`${section.slug}-${i}`} q={f.q} a={f.a} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
