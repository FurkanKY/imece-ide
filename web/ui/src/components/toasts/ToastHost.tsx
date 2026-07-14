/* ToastHost — sağ altta toast yığını; motion ile giriş/çıkış. */

import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { CheckCircle2, AlertCircle, Info, X } from "lucide-react";
import { useToasts, ToastKind } from "./toasts";
import { useSettings } from "@/state/settings";
import { IconButton } from "@/components/ui";

const ICONS: Record<ToastKind, { Icon: typeof Info; cls: string }> = {
  ok: { Icon: CheckCircle2, cls: "text-ok" },
  err: { Icon: AlertCircle, cls: "text-err" },
  info: { Icon: Info, cls: "text-accent" },
};

export function ToastHost() {
  const { toasts, dismiss } = useToasts();
  const reduceMotion = useReducedMotion();
  const animationsEnabled = useSettings((s) => s.prefs?.animations ?? true);
  const animate = animationsEnabled && !reduceMotion;

  return (
    <div className="pointer-events-none fixed bottom-9 right-4 z-[200] flex w-[340px] flex-col gap-2">
      <AnimatePresence>
        {toasts.map((t) => {
          const { Icon, cls } = ICONS[t.kind];
          return (
            <motion.div
              key={t.id}
              initial={{ opacity: 0, transform: animate ? "translateY(12px) scale(0.98)" : "none" }}
              animate={{ opacity: 1, transform: "translateY(0) scale(1)" }}
              exit={{ opacity: 0, transform: animate ? "translateY(8px) scale(0.98)" : "none" }}
              transition={{ duration: 0.18, ease: [0.33, 1, 0.68, 1] }}
              className="material-card pointer-events-auto flex items-start gap-2.5 rounded-[var(--r-md)] border border-border-w px-3.5 py-2.5"
              style={{ boxShadow: "var(--bevel-strong), var(--shadow-2)" }}
            >
              <Icon size={16} className={cls + " mt-px shrink-0"} strokeWidth={2} />
              <p className="min-w-0 flex-1 break-words text-text2" style={{ fontSize: "var(--t-label)" }}>
                {t.text}
              </p>
              {t.action && (
                <button onClick={() => { t.action?.run(); dismiss(t.id); }} className="pressable shrink-0 rounded-[var(--r-sm)] bg-accentdim px-2 py-1 text-accent" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
                  {t.action.label}
                </button>
              )}
              <IconButton size="sm" icon={X} label="Bildirimi kapat" className="shrink-0" onClick={() => dismiss(t.id)} />
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
