/* EmptyState — kısa, yargılamayan boş durum: ikon + başlık + açıklama + tek eylem.
   Explorer/Search/SCM/Agent boş yüzeyleri için tek dil. */

import { cx } from "@/lib/cx";
import type { LucideIcon } from "lucide-react";

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cx(
        "flex flex-col items-center justify-center gap-2 px-6 py-10 text-center",
        className,
      )}
    >
      {Icon && (
        <div className="mb-1 flex size-9 items-center justify-center rounded-[var(--r-md)] bg-card text-faint">
          <Icon size={18} strokeWidth={1.7} />
        </div>
      )}
      <p className="text-text2" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>
        {title}
      </p>
      {description && (
        <p className="max-w-[34ch] text-faint" style={{ fontSize: "var(--t-caption)", lineHeight: 1.5 }}>
          {description}
        </p>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
