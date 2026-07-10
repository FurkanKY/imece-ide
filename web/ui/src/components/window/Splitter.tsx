/* Splitter — paneller arası sürüklenebilir ayırıcı (P4 cila).
   Pointer capture ile sürükleme + klavye erişimi (ok tuşları, role="separator").
   Görsel: 1px hairline; hover/sürüklemede accent'e döner (VS Code deseni). */

import { useCallback, useRef, useState } from "react";
import { useUi } from "@/state/ui";

interface SplitterProps {
  /** "col" = dikey çubuk, genişlik sürükler; "row" = yatay çubuk, yükseklik sürükler */
  orientation: "col" | "row";
  /** sürükleme yönü tersse (sağ/alt panel: sola/yukarı çekmek BÜYÜTÜR) */
  reverse?: boolean;
  value: number;
  min: number;
  max: number;
  onChange: (v: number) => void;
  /** erişilebilirlik etiketi (ör. "Gezgin genişliği") */
  label: string;
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export function Splitter({ orientation, reverse, value, min, max, onChange, label }: SplitterProps) {
  const [active, setActive] = useState(false);
  const start = useRef({ pos: 0, value: 0 });
  const isCol = orientation === "col";
  const setResizing = useUi((s) => s.setResizing);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.currentTarget.setPointerCapture(e.pointerId);
      start.current = { pos: isCol ? e.clientX : e.clientY, value };
      setActive(true);
      setResizing(true);
    },
    [isCol, value, setResizing],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!e.currentTarget.hasPointerCapture(e.pointerId)) return;
      const pos = isCol ? e.clientX : e.clientY;
      let d = pos - start.current.pos;
      if (reverse) d = -d;
      onChange(clamp(start.current.value + d, min, max));
    },
    [isCol, reverse, min, max, onChange],
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      e.currentTarget.releasePointerCapture(e.pointerId);
      setActive(false);
      setResizing(false);
    },
    [setResizing],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const grow = isCol ? (reverse ? "ArrowLeft" : "ArrowRight") : reverse ? "ArrowUp" : "ArrowDown";
      const shrink = isCol ? (reverse ? "ArrowRight" : "ArrowLeft") : reverse ? "ArrowDown" : "ArrowUp";
      if (e.key === grow) onChange(clamp(value + 16, min, max));
      else if (e.key === shrink) onChange(clamp(value - 16, min, max));
      else return;
      e.preventDefault();
    },
    [isCol, reverse, value, min, max, onChange],
  );

  return (
    <div
      role="separator"
      aria-orientation={isCol ? "vertical" : "horizontal"}
      aria-label={label}
      aria-valuenow={Math.round(value)}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onKeyDown={onKeyDown}
      className={
        "relative z-10 shrink-0 select-none " +
        (isCol ? "w-px cursor-col-resize" : "h-px cursor-row-resize")
      }
      style={{ background: "var(--border)" }}
    >
      {/* geniş görünmez tutma alanı + hover/aktif accent vurgusu (kolay yakalama) */}
      <div
        className={
          "absolute transition-colors duration-150 " +
          (isCol ? "-left-[4.5px] top-0 h-full w-[10px]" : "-top-[4.5px] left-0 h-[10px] w-full")
        }
        style={{
          background: active
            ? "color-mix(in srgb, var(--accent) 55%, transparent)"
            : undefined,
        }}
        onMouseEnter={(e) => {
          if (!active) e.currentTarget.style.background = "color-mix(in srgb, var(--accent) 30%, transparent)";
        }}
        onMouseLeave={(e) => {
          if (!active) e.currentTarget.style.background = "";
        }}
      />
    </div>
  );
}
