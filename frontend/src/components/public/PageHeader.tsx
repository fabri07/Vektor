/**
 * PageHeader — encabezado consistente para las páginas de marketing.
 * Título grande font-display + eyebrow opcional + subtítulo.
 */

import type { ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  subtitle,
}: {
  eyebrow?: string;
  title: string;
  subtitle?: ReactNode;
}) {
  return (
    <header className="mx-auto max-w-3xl px-6 pt-20 pb-10 text-center">
      {eyebrow && (
        <p className="mb-4 text-xs font-semibold uppercase tracking-[0.22em] text-vektor-teal">
          {eyebrow}
        </p>
      )}
      <h1 className="font-display text-4xl font-bold uppercase leading-tight tracking-tight text-vektor-white sm:text-5xl">
        {title}
      </h1>
      {subtitle && (
        <p className="mt-5 text-lg leading-8 text-vektor-body">{subtitle}</p>
      )}
    </header>
  );
}
