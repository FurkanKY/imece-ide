/* DialogHost — temalı modal: karartılmış arka plan + gölgeli kart.
   Enter=onay, Esc=iptal; prompt'ta canlı doğrulama. */

import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { useDialogs } from "./dialogs";

export function DialogHost() {
  const { current, close } = useDialogs();
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

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

  if (!current) return <AnimatePresence />;

  const cancel = () => {
    if (current.kind === "confirm") current.resolve(false);
    else current.resolve(null);
    close();
  };

  const ok = () => {
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
      <motion.div
        key="overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="fixed inset-0 z-[150] flex items-start justify-center bg-black/50 pt-[18vh]"
        onPointerDown={(e) => {
          if (e.target === e.currentTarget) cancel();
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") cancel();
          if (e.key === "Enter") ok();
        }}
      >
        <motion.div
          initial={{ opacity: 0, y: 10, scale: 0.98 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 6, scale: 0.98 }}
          transition={{ duration: 0.18, ease: [0.33, 1, 0.68, 1] }}
          className="w-[420px] rounded-[var(--r-lg)] border border-border-w bg-panel p-4"
          style={{ boxShadow: "var(--shadow-3)" }}
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
            <button
              onClick={cancel}
              className="rounded-[var(--r-sm)] border border-border-w bg-transparent px-3.5 py-1.5 text-text2 transition-colors hover:bg-card"
              style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
            >
              Vazgeç
            </button>
            <button
              onClick={ok}
              autoFocus={current.kind === "confirm"}
              className={
                "rounded-[var(--r-sm)] px-3.5 py-1.5 transition-colors " +
                (current.kind === "confirm" && current.danger
                  ? "bg-err text-white hover:brightness-110"
                  : "bg-accent text-on-accent hover:bg-accent2")
              }
              style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
            >
              {current.okLabel}
            </button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
