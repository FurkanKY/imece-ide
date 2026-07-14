/* Select — temalı açılır seçici (Radix Select sarmalayıcısı).
   Native <select>'in OS-gri menüsünün halefi; her yerde tek görünüm. */

import { Select as RSelect } from "radix-ui";
import { Check, ChevronDown } from "lucide-react";

export function Select({
  value,
  options,
  onChange,
  icon,
  ariaLabel,
}: {
  value: string;
  options: string[];
  onChange: (v: string) => void;
  icon?: React.ReactNode;
  ariaLabel?: string;
}) {
  return (
    <RSelect.Root value={value} onValueChange={onChange}>
      <RSelect.Trigger
        aria-label={ariaLabel}
        className="pressable flex min-w-0 flex-1 items-center gap-1.5 rounded-[var(--r-sm)] border border-border-w bg-field px-2 py-1 text-text2 outline-none hover:border-border-w2 focus-visible:border-accent data-[state=open]:border-accent"
        style={{ fontSize: "var(--t-caption)" }}
      >
        {icon}
        <span className="min-w-0 flex-1 truncate text-left">
          <RSelect.Value />
        </span>
        <RSelect.Icon>
          <ChevronDown size={11} className="shrink-0 text-faint" />
        </RSelect.Icon>
      </RSelect.Trigger>
      <RSelect.Portal>
        <RSelect.Content
          position="popper"
          sideOffset={4}
          className="material-panel z-[var(--z-select)] min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded-[var(--r-md)] border border-border-w p-1"
          style={{ boxShadow: "var(--bevel-strong), var(--shadow-2)", transformOrigin: "var(--radix-select-content-transform-origin)" }}
        >
          <RSelect.Viewport>
            {options.map((o) => (
              <RSelect.Item
                key={o}
                value={o}
                className="flex cursor-default select-none items-center gap-2 rounded-[var(--r-xs)] px-2 py-1.5 text-text2 outline-none data-[highlighted]:bg-card2 data-[highlighted]:text-text"
                style={{ fontSize: "var(--t-caption)" }}
              >
                <RSelect.ItemText>{o}</RSelect.ItemText>
                <span className="flex-1" />
                <RSelect.ItemIndicator>
                  <Check size={12} className="text-accent" />
                </RSelect.ItemIndicator>
              </RSelect.Item>
            ))}
          </RSelect.Viewport>
        </RSelect.Content>
      </RSelect.Portal>
    </RSelect.Root>
  );
}
