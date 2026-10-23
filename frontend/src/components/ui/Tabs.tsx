"use client";

import { useEffect, useRef, useState } from "react";

export interface TabItem {
  id: string;
  label: string;
}

interface TabsProps {
  tabs: TabItem[];
  activeTab: string;
  onChange: (id: string) => void;
  variant?: "underline" | "pill";
}

// ── Variante underline ────────────────────────────────────────────────────────

function UnderlineTabs({ tabs, activeTab, onChange }: Omit<TabsProps, "variant">) {
  const listRef = useRef<HTMLDivElement>(null);
  const [indicator, setIndicator] = useState({ left: 0, width: 0 });

  // Calcular posición del indicador a partir del tab activo
  useEffect(() => {
    const list = listRef.current;
    if (!list) return;
    const activeEl = list.querySelector<HTMLButtonElement>(`[data-tab-id="${activeTab}"]`);
    if (!activeEl) return;
    setIndicator({ left: activeEl.offsetLeft, width: activeEl.offsetWidth });
  }, [activeTab, tabs]);

  function handleKeyDown(e: React.KeyboardEvent, currentIdx: number) {
    if (e.key === "ArrowRight") {
      const next = tabs[(currentIdx + 1) % tabs.length];
      if (next) onChange(next.id);
    } else if (e.key === "ArrowLeft") {
      const prev = tabs[(currentIdx - 1 + tabs.length) % tabs.length];
      if (prev) onChange(prev.id);
    }
  }

  return (
    <div
      ref={listRef}
      role="tablist"
      className="relative flex border-b border-vk-border-w"
    >
      {tabs.map((tab, idx) => {
        const isActive = tab.id === activeTab;
        return (
          <button
            key={tab.id}
            data-tab-id={tab.id}
            role="tab"
            aria-selected={isActive}
            tabIndex={isActive ? 0 : -1}
            onClick={() => onChange(tab.id)}
            onKeyDown={(e) => handleKeyDown(e, idx)}
            className={[
              "px-4 pb-3 pt-2.5 text-sm font-medium transition-colors focus:outline-none",
              isActive
                ? "text-vk-text-primary"
                : "text-vk-text-muted hover:text-vk-text-secondary",
            ].join(" ")}
          >
            {tab.label}
          </button>
        );
      })}

      {/* Indicador animado */}
      <div
        aria-hidden="true"
        className="absolute bottom-0 h-0.5 rounded-full bg-vk-blue transition-all duration-200"
        style={{ left: indicator.left, width: indicator.width }}
      />
    </div>
  );
}

// ── Variante pill ─────────────────────────────────────────────────────────────

function PillTabs({ tabs, activeTab, onChange }: Omit<TabsProps, "variant">) {
  function handleKeyDown(e: React.KeyboardEvent, currentIdx: number) {
    if (e.key === "ArrowRight") {
      const next = tabs[(currentIdx + 1) % tabs.length];
      if (next) onChange(next.id);
    } else if (e.key === "ArrowLeft") {
      const prev = tabs[(currentIdx - 1 + tabs.length) % tabs.length];
      if (prev) onChange(prev.id);
    }
  }

  return (
    <div
      role="tablist"
      className="inline-flex gap-1 rounded-lg bg-vk-bg-light p-1"
    >
      {tabs.map((tab, idx) => {
        const isActive = tab.id === activeTab;
        return (
          <button
            key={tab.id}
            role="tab"
            aria-selected={isActive}
            tabIndex={isActive ? 0 : -1}
            onClick={() => onChange(tab.id)}
            onKeyDown={(e) => handleKeyDown(e, idx)}
            className={[
              "rounded-md px-4 py-2 text-sm font-medium transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-vk-blue/20",
              isActive
                ? "bg-vk-blue-subtle text-vk-blue shadow-vk-sm"
                : "text-vk-text-muted hover:text-vk-text-secondary",
            ].join(" ")}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

// ── Export ────────────────────────────────────────────────────────────────────

export function Tabs({ tabs, activeTab, onChange, variant = "underline" }: TabsProps) {
  if (variant === "pill") {
    return <PillTabs tabs={tabs} activeTab={activeTab} onChange={onChange} />;
  }
  return <UnderlineTabs tabs={tabs} activeTab={activeTab} onChange={onChange} />;
}
