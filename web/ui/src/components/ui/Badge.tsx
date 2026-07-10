/* Badge — kompakt sayı/etiket kapsülü (diff sayısı, rozet, durum etiketi).
   Ton: neutral | accent | ok | warn | err | info. */

import { cx } from "@/lib/cx";

type Tone = "neutral" | "accent" | "ok" | "warn" | "err" | "info";

const TONE: Record<Tone, string> = {
  neutral: "bg-card2 text-muted",
  accent: "bg-accentdim text-accent",
  ok: "bg-ok/15 text-ok",
  warn: "bg-warn/15 text-warn",
  err: "bg-err/15 text-err",
  info: "bg-info/15 text-info",
};

export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: React.ReactNode;
  tone?: Tone;
  className?: string;
}) {
  return (
    <span
      className={cx(
        "inline-flex min-w-[16px] items-center justify-center gap-1 rounded-[var(--r-pill)] px-1.5 py-0.5 leading-none",
        TONE[tone],
        className,
      )}
      style={{ fontSize: "var(--t-caption)", fontWeight: 700 }}
    >
      {children}
    </span>
  );
}
