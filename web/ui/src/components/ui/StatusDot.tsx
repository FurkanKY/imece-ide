/* StatusDot — semantik durum noktası (idle/running/done/error).
   Renk her zaman metin/ikon ile desteklenmeli (yalnız renge güvenme kuralı);
   bu yüzden StatusDot tek başına anlam taşımaz, etiketin yanında kullanılır. */

import { cx } from "@/lib/cx";

type Tone = "neutral" | "accent" | "ok" | "warn" | "err";

const TONE: Record<Tone, string> = {
  neutral: "bg-faint",
  accent: "bg-accent",
  ok: "bg-ok",
  warn: "bg-warn",
  err: "bg-err",
};

export function StatusDot({
  tone = "neutral",
  pulse = false,
  size = 7,
  className,
}: {
  tone?: Tone;
  pulse?: boolean;
  size?: number;
  className?: string;
}) {
  return (
    <span className={cx("relative inline-flex shrink-0", className)} style={{ width: size, height: size }} aria-hidden>
      {pulse && (
        <span
          className="absolute inset-0 animate-ping rounded-[var(--r-pill)] opacity-70 motion-reduce:animate-none"
          style={{ background: "currentColor" }}
        />
      )}
      <span className={cx("relative inline-block rounded-[var(--r-pill)]", TONE[tone])} style={{ width: size, height: size }} />
    </span>
  );
}
