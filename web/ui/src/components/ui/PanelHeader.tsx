/* PanelHeader — tüm kenar/alt panellerin ortak, sakin başlık satırı. Sabit
   yükseklik layout shift'i önler; yüzey yerine ayracı hiyerarşi kurar. */

import { cx } from "@/lib/cx";
import type { LucideIcon } from "lucide-react";

export function PanelHeader({
  title,
  icon: Icon,
  actions,
  className,
}: {
  title: string;
  icon?: LucideIcon;
  actions?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cx(
        "flex h-10 shrink-0 items-center justify-between gap-2 border-b border-border-w bg-side px-3",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-1.5 text-muted">
        {Icon && <Icon size={13} strokeWidth={1.9} className="shrink-0" />}
        <span
          className="truncate"
          style={{
            fontSize: "var(--t-caption)",
            fontWeight: "var(--w-label)",
          }}
        >
          {title}
        </span>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
    </div>
  );
}
