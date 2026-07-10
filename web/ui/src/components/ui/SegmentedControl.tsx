/* SegmentedControl — 2+ seçenek arasında tek seçim (örn. diff inline/side-by-side,
   panel sekmeleri). Klavye: ok tuşları roving tabindex ile. */

import { cx } from "@/lib/cx";

export interface Segment<T extends string> {
  value: T;
  label: string;
  icon?: React.ReactNode;
}

export function SegmentedControl<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
  className,
}: {
  value: T;
  options: Segment<T>[];
  onChange: (v: T) => void;
  ariaLabel?: string;
  className?: string;
}) {
  const idx = options.findIndex((o) => o.value === value);
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      onChange(options[(idx + 1) % options.length].value);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      onChange(options[(idx - 1 + options.length) % options.length].value);
    }
  };
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cx("inline-flex items-center gap-0.5 rounded-[var(--r-sm)] border border-border-w bg-field p-0.5", className)}
    >
      {options.map((o) => {
        const on = o.value === value;
        return (
          <button
            key={o.value}
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            onKeyDown={onKey}
            onClick={() => onChange(o.value)}
            className={cx(
              "pressable inline-flex items-center gap-1.5 rounded-[var(--r-xs)] px-2 py-1 outline-none",
              on ? "bg-card2 text-text" : "text-muted hover:text-text2",
            )}
            style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}
          >
            {o.icon}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
