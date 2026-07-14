/* DialogHost — temalı modal: karartılmış arka plan + gölgeli kart.
   Enter=onay, Esc=iptal; prompt'ta canlı doğrulama. */

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useDialogs } from "./dialogs";
import { Button } from "@/components/ui";
import { useSettings } from "@/state/settings";

export function DialogHost() {
  const { current, close } = useDialogs();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const reduceMotion = useReducedMotion();
  const animationsEnabled = useSettings((s) => s.prefs?.animations ?? true);
  const animate = animationsEnabled && !reduceMotion;

  useEffect(() => {
    if (current?.kind === "prompt") {
      setValue(current.initial);
      setError(null);
      // mount sonrası odak + ad seçimi (uzantı hariç)
      requestAnimationFrame(() => {
        const el = inputRef.current;
        if (!el) return;
        el.focus();
        const dot = current.initial.lastIndexOf(".");
        el.setSelectionRange(0, dot > 0 ? dot : current.initial.length);
      });
    }
  }, [current]);

  const cancel = () => {
    if (!current) return;
    if (current.kind === "confirm") current.resolve(false);
    else current.resolve(null);
    close();
  };

  const ok = () => {
    if (!current) return;
    if (current.kind === "confirm") {
      current.resolve(true);
      close();
      return;
    }
    const err = current.validate?.(value) ?? null;
    if (err) {
      setError(err);
      return;
    }
    current.resolve(value);
    close();
  };

  return (
    <AnimatePresence>
      {current && <motion.div
        key="overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: animate ? 0.12 : 0 }}
        className="fixed inset-0 z-[150] flex items-center justify-center overflow-y-auto bg-black/50 p-4"
        onPointerDown={(e) => {
          if (e.target === e.currentTarget) cancel();
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") cancel();
          if (e.key === "Enter") ok();
        }}
      >
        <motion.div
          initial={{ opacity: 0, transform: "translateY(10px) scale(0.98)" }}
          animate={{ opacity: 1, transform: "translateY(0) scale(1)" }}
          exit={{ opacity: 0, transform: "translateY(6px) scale(0.98)" }}
          transition={{ duration: animate ? 0.18 : 0, ease: [0.33, 1, 0.68, 1] }}
          className="material-panel max-h-full w-[min(420px,calc(100vw-2rem))] overflow-y-auto rounded-[var(--r-lg)] border border-border-w p-4"
          style={{ boxShadow: "var(--bevel-strong), var(--shadow-3)" }}
          role="dialog"
          aria-modal="true"
        >
          <h2
            className="text-text"
            style={{ fontSize: "var(--t-title)", fontWeight: "var(--w-title)" }}
          >
            {current.title}
          </h2>
          {current.message && (
            <p className="mt-1.5 text-muted" style={{ fontSize: "var(--t-body)" }}>
              {current.message}
            </p>
          )}

          {current.kind === "prompt" && (
            <div className="mt-3">
              <input
                ref={inputRef}
                value={value}
                onChange={(e) => {
                  setValue(e.target.value);
                  if (error) setError(null);
                }}
                placeholder={current.placeholder}
                spellCheck={false}
                className={
                  "selectable w-full rounded-[var(--r-sm)] border bg-field px-3 py-2 text-text outline-none transition-colors " +
                  (error ? "border-err" : "border-border-w2 focus:border-accent")
                }
                style={{ fontSize: "var(--t-body)" }}
              />
              {error && (
                <p className="mt-1.5 text-err" style={{ fontSize: "var(--t-caption)" }}>
                  {error}
                </p>
              )}
            </div>
          )}

          <div className="mt-4 flex justify-end gap-2">
            <Button variant="secondary" size="sm" onClick={cancel}>
              Vazgeç
            </Button>
            <Button
              variant={current.kind === "confirm" && current.danger ? "danger" : "primary"}
              size="sm"
              onClick={ok}
              autoFocus={current.kind === "confirm"}
            >
              {current.okLabel}
            </Button>
          </div>
        </motion.div>
      </motion.div>}
    </AnimatePresence>
  );
}
