"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import {
  LayoutDashboard,
  ShoppingCart,
  Receipt,
  Package,
  Upload,
  Settings,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { api } from "@/lib/api";
import { VektorLogo } from "@/components/ui/VektorLogo";

interface SidebarProps {
  mobileOpen: boolean;
  onClose: () => void;
}

const NAV_ITEMS = [
  { label: "Dashboard",     href: "/dashboard", icon: LayoutDashboard },
  { label: "Ventas",        href: "/sales",      icon: ShoppingCart },
  { label: "Gastos",        href: "/expenses",   icon: Receipt },
  { label: "Productos",     href: "/products",   icon: Package },
  { label: "Cargar datos",  href: "/ingestion",  icon: Upload },
];

function getInitials(name: string): string {
  return name
    .split(" ")
    .map((n) => n[0] ?? "")
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

export function Sidebar({ mobileOpen, onClose }: SidebarProps) {
  const pathname = usePathname();
  const user = useAuthStore((s) => s.user);
  // Default false; useEffect sets true on tablet (md < lg) after hydration
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    if (window.innerWidth >= 768 && window.innerWidth < 1024) {
      setCollapsed(true);
    }
  }, []);

  const { data: profileData } = useQuery({
    queryKey: ["business-profile"],
    queryFn: async () => {
      const res = await api.get<{ business_name: string }[]>("/business_profiles/");
      return res.data[0] ?? null;
    },
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  const businessName = profileData?.business_name ?? "Mi negocio";
  const initials = getInitials(user?.full_name ?? user?.email ?? "U");

  return (
    <aside
      className={clsx(
        "flex h-full flex-col bg-vk-bg-dark",
        "transition-all duration-200 ease-in-out",
        // Mobile: fixed drawer
        "fixed inset-y-0 left-0 z-50 w-[260px]",
        mobileOpen ? "translate-x-0 shadow-vk-lg" : "-translate-x-full",
        // md+: static, visible
        "md:relative md:inset-auto md:z-auto md:translate-x-0 md:shadow-none",
        // Width: tablet colapsable, desktop always 260
        collapsed ? "md:w-[60px] lg:w-[260px]" : "md:w-[260px]",
      )}
    >
      {/* ── Logo + business name ── */}
      <div
        className={clsx(
          "flex h-14 items-center border-b border-vk-border-dark px-4",
          collapsed ? "md:justify-center lg:justify-start lg:px-4" : "justify-start",
        )}
      >
        <Link
          href="/dashboard"
          onClick={onClose}
          className={clsx(
            "flex items-center focus:outline-none focus-visible:ring-2 focus-visible:ring-vk-blue/60 rounded",
            collapsed && "md:justify-center lg:justify-start",
          )}
        >
          {/* Collapsed (tablet only): icon only */}
          <span className={clsx(collapsed ? "md:block lg:hidden" : "hidden")}>
            <VektorLogo variant="icon" size="md" theme="dark" />
          </span>
          {/* Expanded: full logo */}
          <span className={collapsed ? "md:hidden lg:flex" : "flex"}>
            <VektorLogo variant="full" size="md" theme="dark" />
          </span>
        </Link>
      </div>

      {/* ── Business name ── */}
      {!collapsed && (
        <div className="px-4 py-2.5 border-b border-vk-border-dark">
          <p className="text-xs font-medium text-vk-text-muted truncate">{businessName}</p>
        </div>
      )}

      {/* ── Nav items ── */}
      <nav className="flex-1 overflow-y-auto scrollbar-thin px-2 py-3">
        <ul className="space-y-0.5">
          {NAV_ITEMS.map((item) => {
            const isActive =
              pathname === item.href || pathname.startsWith(item.href + "/");
            const Icon = item.icon;

            return (
              <li key={item.href}>
                <div className="group relative">
                  <Link
                    href={item.href}
                    onClick={onClose}
                    className={clsx(
                      "flex items-center gap-3 rounded-md px-3 py-2.5 text-sm font-medium",
                      "transition-[colors,padding-left] duration-[150ms]",
                      "focus:outline-none focus-visible:ring-2 focus-visible:ring-vk-blue/60",
                      isActive
                        ? "bg-vk-surface-1 border-l-[3px] border-l-vk-blue text-vk-text-light pl-[9px]"
                        : "border-l-[3px] border-l-transparent text-vk-text-muted hover:bg-vk-surface-1 hover:text-vk-text-light hover:pl-[13px] pl-[9px]",
                      collapsed && "md:justify-center md:px-0 md:pl-0 md:hover:pl-0 lg:justify-start lg:px-3 lg:pl-[9px] lg:hover:pl-[13px]",
                    )}
                  >
                    <Icon className="h-[18px] w-[18px] shrink-0" />
                    <span
                      className={clsx(
                        "truncate",
                        collapsed && "md:hidden lg:block",
                      )}
                    >
                      {item.label}
                    </span>
                  </Link>
                  {/* Tooltip — visible only when collapsed on tablet */}
                  {collapsed && (
                    <span
                      className={clsx(
                        "pointer-events-none absolute left-full top-1/2 z-50 ml-2 -translate-y-1/2",
                        "whitespace-nowrap rounded-md bg-vk-surface-2 px-2.5 py-1.5",
                        "text-xs font-medium text-vk-text-light shadow-vk-md",
                        "opacity-0 transition-opacity duration-150 group-hover:opacity-100",
                        "hidden md:block lg:hidden",
                      )}
                    >
                      {item.label}
                    </span>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </nav>

      {/* ── Tablet collapse toggle ── */}
      <div className="hidden md:flex lg:hidden items-center justify-center border-t border-vk-border-dark py-2">
        <button
          onClick={() => setCollapsed((v) => !v)}
          className="flex h-8 w-8 items-center justify-center rounded-md text-vk-text-muted hover:bg-vk-surface-1 hover:text-vk-text-light transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-vk-blue/60"
          aria-label={collapsed ? "Expandir sidebar" : "Colapsar sidebar"}
        >
          {collapsed ? (
            <ChevronRight className="h-4 w-4" />
          ) : (
            <ChevronLeft className="h-4 w-4" />
          )}
        </button>
      </div>

      {/* ── Bottom section: user + settings ── */}
      <div className="border-t border-vk-border-dark p-3">
        {/* Settings link */}
        <div className="group relative mb-2">
          <Link
            href="/settings"
            onClick={onClose}
            className={clsx(
              "flex items-center gap-3 rounded-md px-3 py-2.5 text-sm font-medium",
              "border-l-[3px] transition-[colors,padding-left] duration-[150ms]",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-vk-blue/60",
              pathname === "/settings"
                ? "bg-vk-surface-1 border-l-vk-blue text-vk-text-light pl-[9px]"
                : "border-l-transparent text-vk-text-muted hover:bg-vk-surface-1 hover:text-vk-text-light hover:pl-[13px] pl-[9px]",
              collapsed && "md:justify-center md:px-0 md:pl-0 md:hover:pl-0 lg:justify-start lg:px-3 lg:pl-[9px] lg:hover:pl-[13px]",
            )}
          >
            <Settings className="h-[18px] w-[18px] shrink-0" />
            <span className={clsx("truncate", collapsed && "md:hidden lg:block")}>
              Configuración
            </span>
          </Link>
          {collapsed && (
            <span
              className={clsx(
                "pointer-events-none absolute left-full top-1/2 z-50 ml-2 -translate-y-1/2",
                "whitespace-nowrap rounded-md bg-vk-surface-2 px-2.5 py-1.5",
                "text-xs font-medium text-vk-text-light shadow-vk-md",
                "opacity-0 transition-opacity duration-150 group-hover:opacity-100",
                "hidden md:block lg:hidden",
              )}
            >
              Configuración
            </span>
          )}
        </div>

        {/* User info */}
        <div
          className={clsx(
            "flex items-center gap-2.5 rounded-md px-2 py-2",
            collapsed && "md:justify-center lg:justify-start",
          )}
        >
          {/* Avatar */}
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-vk-blue/20 text-xs font-bold text-vk-blue-light">
            {initials}
          </div>

          {/* Email + plan badge */}
          <div
            className={clsx(
              "min-w-0 flex-1",
              collapsed && "md:hidden lg:block",
            )}
          >
            <p className="truncate text-xs font-medium text-vk-text-light">
              {user?.email ?? ""}
            </p>
            <span className="inline-flex items-center rounded-full bg-vk-blue/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-vk-blue-light">
              {user?.role ?? "user"}
            </span>
          </div>
        </div>
      </div>
    </aside>
  );
}
