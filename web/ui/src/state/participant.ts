import { create } from "zustand";
import type { ParticipantSession } from "@/bridge";

interface ParticipantState {
  projectRoot: string | null;
  session: ParticipantSession | null;
  message: string;
  busy: boolean;
  setSession: (session: ParticipantSession | null) => void;
  setMessage: (message: string) => void;
  begin: () => number | null;
  finish: (token: number) => void;
  resetRoot: (root: string | null) => void;
}

let nextFlight = 0;
let activeFlight: number | null = null;

export const useParticipant = create<ParticipantState>((set, get) => ({
  projectRoot: null,
  session: null,
  message: "",
  busy: false,
  setSession: (session) => set({ session }),
  setMessage: (message) => set({ message }),
  begin: () => {
    if (get().busy) return null;
    const token = ++nextFlight;
    activeFlight = token;
    set({ busy: true });
    return token;
  },
  finish: (token) => {
    if (activeFlight !== token) return;
    activeFlight = null;
    set({ busy: false });
  },
  resetRoot: (root) => {
    const state = get();
    if (state.projectRoot === root) return;
    activeFlight = null;
    set({ projectRoot: root, session: null, message: "", busy: false });
  },
}));
