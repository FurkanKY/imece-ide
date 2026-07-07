/* dialogs.ts — promise tabanlı temalı diyaloglar. Eski 9 native QMessageBox/QInputDialog
   çağrısının halefi: `await confirmDialog(...)`, `await promptDialog(...)`. */

import { create } from "zustand";

interface ConfirmSpec {
  kind: "confirm";
  title: string;
  message: string;
  okLabel: string;
  danger: boolean;
  resolve: (ok: boolean) => void;
}

interface PromptSpec {
  kind: "prompt";
  title: string;
  message: string;
  okLabel: string;
  initial: string;
  placeholder: string;
  validate?: (v: string) => string | null; // hata metni | null=geçerli
  resolve: (value: string | null) => void;
}

export type DialogSpec = ConfirmSpec | PromptSpec;

interface DialogState {
  current: DialogSpec | null;
  open: (spec: DialogSpec) => void;
  close: () => void;
}

export const useDialogs = create<DialogState>((set) => ({
  current: null,
  open: (spec) => set({ current: spec }),
  close: () => set({ current: null }),
}));

export function confirmDialog(opts: {
  title: string;
  message: string;
  okLabel?: string;
  danger?: boolean;
}): Promise<boolean> {
  return new Promise((resolve) => {
    useDialogs.getState().open({
      kind: "confirm",
      title: opts.title,
      message: opts.message,
      okLabel: opts.okLabel ?? "Tamam",
      danger: opts.danger ?? false,
      resolve,
    });
  });
}

export function promptDialog(opts: {
  title: string;
  message?: string;
  okLabel?: string;
  initial?: string;
  placeholder?: string;
  validate?: (v: string) => string | null;
}): Promise<string | null> {
  return new Promise((resolve) => {
    useDialogs.getState().open({
      kind: "prompt",
      title: opts.title,
      message: opts.message ?? "",
      okLabel: opts.okLabel ?? "Tamam",
      initial: opts.initial ?? "",
      placeholder: opts.placeholder ?? "",
      validate: opts.validate,
      resolve,
    });
  });
}
