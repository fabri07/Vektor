"use client";

import {
  type ReactNode,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";

type TooltipPosition = "top" | "bottom" | "left" | "right";

interface TooltipProps {
  children: ReactNode;
  content: string | ReactNode;
  position?: TooltipPosition;
}

const POSITION_STYLES: Record<
  TooltipPosition,
  {
    container: string;
    arrow: string;
  }
> = {
  top: {
    container: "bottom-full left-1/2 mb-3 -translate-x-1/2",
    arrow: "left-1/2 top-full -translate-x-1/2",
  },
  bottom: {
    container: "left-1/2 top-full mt-3 -translate-x-1/2",
    arrow: "bottom-full left-1/2 -translate-x-1/2",
  },
  left: {
    container: "right-full top-1/2 mr-3 -translate-y-1/2",
    arrow: "left-full top-1/2 -translate-y-1/2",
  },
  right: {
    container: "left-full top-1/2 ml-3 -translate-y-1/2",
    arrow: "right-full top-1/2 -translate-y-1/2",
  },
};

export function Tooltip({
  children,
  content,
  position = "top",
}: TooltipProps) {
  const [isVisible, setIsVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tooltipId = useId();
  const { container, arrow } = POSITION_STYLES[position];

  useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    };
  }, []);

  function handleEnter() {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    timerRef.current = setTimeout(() => {
      setIsVisible(true);
    }, 300);
  }

  function handleLeave() {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    setIsVisible(false);
  }

  return (
    <span
      className="relative inline-flex"
      onMouseEnter={handleEnter}
      onMouseLeave={handleLeave}
      aria-describedby={isVisible ? tooltipId : undefined}
    >
      {children}

      {isVisible ? (
        <span
          id={tooltipId}
          role="tooltip"
          className={[
            "pointer-events-none absolute z-[250] max-w-[260px] whitespace-normal",
            "rounded-xl border border-vektor-border bg-vektor-surface p-3 shadow-xl",
            "text-[13px] font-normal leading-5 text-vektor-body",
            "animate-fade-slide-up",
            container,
          ].join(" ")}
        >
          {content}
          <span
            aria-hidden="true"
            className={[
              "absolute h-3 w-3 rotate-45 border-r border-b border-vektor-border bg-vektor-surface",
              arrow,
            ].join(" ")}
          />
        </span>
      ) : null}
    </span>
  );
}
