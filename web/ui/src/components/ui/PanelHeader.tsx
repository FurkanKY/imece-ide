/* PanelHeader — tüm kenar/alt panellerin ortak başlık yapısı: overline başlık +
   opsiyonel ikon + sağda aksiyon alanı. Sabit yükseklik → layout shift olmaz. */

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
        "material-panel flex h-10 shrink-0 items-center justify-between gap-2 border-b border-border-w px-3",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-1.5 text-muted">
        {Icon && <Icon size={13} strokeWidth={1.9} className="shrink-0" />}
        <span
          className="truncate"
          style={{
            fontSize: "var(--t-overline)",
            fontWeight: "var(--w-overline)",
            letterSpacing: "var(--ls-overline)",
          }}
        >
          {title}
        </span>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
    </div>
  );
}
