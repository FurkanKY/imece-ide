/* Kbd — klavye kısayolu ipucu. Palette/menü/tooltip'lerde tutarlı kısayol rozeti.
   Örn. <Kbd>Ctrl</Kbd><Kbd>K</Kbd> ya da <Kbd>Ctrl K</Kbd>. */

import { cx } from "@/lib/cx";

export function Kbd({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <kbd
      className={cx(
        "inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-[var(--r-xs)] border border-border-w bg-card px-1.5 text-faint",
        className,
      )}
      style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)", lineHeight: 1 }}
    >
      {children}
    </kbd>
  );
}
