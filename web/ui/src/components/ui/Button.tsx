/* Button — uygulamanın tek buton primitifi. Mevcut ad-hoc buton desenlerini
   (accent run butonu, apply, danger stop) tek API'de toplar.
   Varyant: primary | secondary | ghost | danger.  Boyut: sm | md.
   Bütün varyantlar focus-visible, disabled, loading ve token renklerini destekler. */

import { forwardRef } from "react";
import { cx } from "@/lib/cx";
import { Spinner } from "./Spinner";
import type { LucideIcon } from "lucide-react";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "danger-outline" | "warn-outline";
type Size = "sm" | "md";

const VARIANT: Record<Variant, string> = {
  primary: "bg-accent text-on-accent hover:bg-accent2 disabled:hover:bg-accent",
  secondary: "border border-border-w bg-transparent text-text2 hover:bg-surface-hover hover:text-text",
  ghost: "text-text2 hover:bg-surface-hover hover:text-text",
  // yıkıcı-birincil (silme/geri alma onayı): dolu kırmızı
  danger: "bg-err text-white hover:brightness-110",
  // yıkıcı-ikincil (koşuyu durdur): kalıcı kırmızı çerçeve, dolgusuz —
  // DECISIONS "Danger buton varyantı": Composer'ın durdur deseni dolu kırmızıdan ayrı kalır
  "danger-outline": "border border-err/50 bg-transparent text-err hover:bg-err/15",
  // dikkat-ikincil (checkpoint geri-al tetikleyicisi): sarı çerçeve; asıl onay
  // DialogHost'ta dolu-kırmızı danger ile alınır
  "warn-outline": "border border-warn/40 bg-transparent text-warn hover:bg-warn/10",
};

const SIZE: Record<Size, string> = {
  sm: "h-7 gap-1.5 px-2.5",
  md: "h-9 gap-2 px-3.5",
};

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  /** başa yerleşen ikon (loading sırasında spinner ile değişir) */
  icon?: LucideIcon;
  /** true → içerik ortalanır ve buton flex-1 genişler */
  block?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading = false, icon: Icon, block, className, children, disabled, style, ...rest },
  ref,
) {
  const shadow = variant === "primary" ? { boxShadow: "var(--shadow-1)" } : undefined;
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cx(
        "pressable inline-flex items-center justify-center rounded-[var(--r-md)] outline-none",
        "disabled:cursor-not-allowed disabled:opacity-55",
        SIZE[size],
        VARIANT[variant],
        block && "flex-1",
        className,
      )}
      style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)", ...shadow, ...style }}
      {...rest}
    >
      {loading ? <Spinner size={size === "sm" ? 13 : 15} /> : Icon ? <Icon size={size === "sm" ? 14 : 15} strokeWidth={2} className="shrink-0" /> : null}
      {children}
    </button>
  );
});
