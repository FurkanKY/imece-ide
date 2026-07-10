/* Divider — hairline ayraç (yatay/dikey). Panel/menü bölümlerini ayırır. */

import { cx } from "@/lib/cx";

export function Divider({
  orientation = "horizontal",
  className,
}: {
  orientation?: "horizontal" | "vertical";
  className?: string;
}) {
  return (
    <div
      role="separator"
      aria-orientation={orientation}
      className={cx(
        orientation === "horizontal" ? "h-px w-full" : "h-full w-px",
        "shrink-0 bg-border-w",
        className,
      )}
    />
  );
}
