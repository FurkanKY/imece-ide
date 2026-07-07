/* toasts.ts — hafif toast store'u. Eski UI'daki tek "hint" etiketinin halefi:
   başarı/hata/bilgi her yerden `toast.ok/err/info(...)` ile yüzeye çıkar. */

import { create } from "zustand";

export type ToastKind = "ok" | "err" | "info";

export interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}

interface ToastState {
  toasts: Toast[];
  push: (kind: ToastKind, text: string, ms?: number) => void;
  dismiss: (id: number) => void;
}

let nextId = 1;

export const useToasts = create<ToastState>((set) => ({
  toasts: [],
  push: (kind, text, ms) => {
    const id = nextId++;
    set((s) => ({ toasts: [...s.toasts, { id, kind, text }] }));
    const ttl = ms ?? (kind === "err" ? 6000 : 3500);
    setTimeout(() => {
      set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
    }, ttl);
  },
  dismiss: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

export const toast = {
  ok: (text: string) => useToasts.getState().push("ok", text),
  err: (text: string) => useToasts.getState().push("err", text),
  info: (text: string) => useToasts.getState().push("info", text),
};
