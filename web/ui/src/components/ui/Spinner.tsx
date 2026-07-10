/* Spinner — token temalı yükleniyor göstergesi. Animasyon tercihi kapalıysa
   (data-animations="off" / reduced-motion) dönmez ama görünür kalır. */

import { Loader2 } from "lucide-react";
import { cx } from "@/lib/cx";

export function Spinner({ size = 14, className }: { size?: number; className?: string }) {
  return (
    <Loader2
      size={size}
      strokeWidth={2.2}
      className={cx("animate-spin motion-reduce:animate-none", className)}
      aria-hidden
    />
  );
}
