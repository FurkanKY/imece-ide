/* Tooltip — Radix tabanlı temalı ipucu. Erişilebilir adın YERİNE geçmez;
   yalnız ek bağlam/kısayol gösterir. Kısa gecikme, animasyonsuz-tercih güvenli. */

import { Tooltip as RTooltip } from "radix-ui";

export function Tooltip({
  children,
  content,
  side = "bottom",
  delay = 350,
}: {
  children: React.ReactNode;
  content: React.ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  delay?: number;
}) {
  return (
    <RTooltip.Provider delayDuration={delay}>
      <RTooltip.Root>
        <RTooltip.Trigger asChild>{children}</RTooltip.Trigger>
        <RTooltip.Portal>
          <RTooltip.Content
            side={side}
            sideOffset={6}
            className="material-panel z-[var(--z-tooltip)] flex items-center gap-2 rounded-[var(--r-sm)] border border-border-w px-2 py-1 text-text2"
            style={{ fontSize: "var(--t-caption)", boxShadow: "var(--bevel-strong), var(--shadow-2)" }}
          >
            {content}
            <RTooltip.Arrow className="fill-[var(--panel2)]" />
          </RTooltip.Content>
        </RTooltip.Portal>
      </RTooltip.Root>
    </RTooltip.Provider>
  );
}
