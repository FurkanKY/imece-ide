/* IconButton — ikon-only aksiyon. Mevcut `.icon-btn` yardımcı sınıfını primitife
   çevirir; erişilebilir ad (aria-label) ZORUNLU. Varyant: ghost | danger. */

import { forwardRef } from "react";
import { cx } from "@/lib/cx";
import type { LucideIcon } from "lucide-react";

type Variant = "ghost" | "danger";

const SIZE = { sm: "size-6", md: "size-7", lg: "size-8" } as const;
const ICON = { sm: 13, md: 14, lg: 16 } as const;

export interface IconButtonProps extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "aria-label"> {
  icon: LucideIcon;
  /** erişilebilir ad — ikon-only kontroller için zorunlu */
  label: string;
  size?: keyof typeof SIZE;
  variant?: Variant;
  active?: boolean;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { icon: Icon, label, size = "md", variant = "ghost", active = false, className, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      aria-label={label}
      title={label}
      aria-pressed={active || undefined}
      className={cx(
        "icon-btn shrink-0 outline-none disabled:cursor-not-allowed disabled:opacity-45",
        SIZE[size],
        variant === "danger" && "hover:!bg-err/15 hover:!text-err",
        active && "!bg-surface-selected !text-accent",
        className,
      )}
      {...rest}
    >
      <Icon size={ICON[size]} strokeWidth={1.9} />
    </button>
  );
});
