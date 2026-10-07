import { create } from "zustand";
import { bridge, CandidateReceipt } from "@/bridge";
import { useWorkspace } from "@/state/workspace";
import { useEditor, type Tab } from "@/state/editor";
import { useRun } from "@/state/run";
import { confirmDialog } from "@/components/dialogs/dialogs";

let root = useWorkspace.getState().root;
let generation = 0;
let mutationRevision = 0;
let refreshTicket = 0;

interface CandidateState {
  candidates: CandidateReceipt[];
  selected: string[];
  busy: boolean;
  error: string;
  toggle: (runId: string) => void;
  refresh: () => Promise<void>;
  prepare: () => Promise<void>;
  apply: (candidateId: string) => Promise<void>;
  rollback: (candidateId: string) => Promise<void>;
}

export const useCandidate = create<CandidateState>((set, get) => ({
  candidates: [], selected: [], busy: false, error: "",
  toggle: (runId) => {
    if (get().busy) return;
    const run = useRun.getState().runs[runId];
    if (!run || run.root !== root || run.readOnly || run.runStage !== "ready" || run.uncertain || run.pending) return;
    set((s) => ({ selected: s.selected.includes(runId)
      ? s.selected.filter((id) => id !== runId) : [...s.selected, runId].slice(-2) }));
  },
  refresh: async () => {
    const capturedRoot = root, token = generation;
    const revision = mutationRevision, ticket = ++refreshTicket;
    if (!capturedRoot) return;
    try {
      const { candidates } = await bridge.call("candidate.list", {});
      if (root !== capturedRoot || token !== generation || revision !== mutationRevision || ticket !== refreshTicket) return;
      if (!Array.isArray(candidates) || candidates.some((c) => c.projectRoot !== capturedRoot)) throw new Error("Aday listesi proje ile eşleşmiyor.");
      set({ candidates });
    } catch (error) {
      if (root === capturedRoot && token === generation && revision === mutationRevision && ticket === refreshTicket) set({ error: error instanceof Error ? error.message : "Aday geçmişi okunamadı." });
    }
  },
  prepare: async () => {
    const { selected, busy } = get();
    if (!root || busy || !selected.length) return;
    const ids = selected.filter((id) => {
      const run = useRun.getState().runs[id];
      return run?.root === root && !run.readOnly && run.runStage === "ready" && !run.uncertain && !run.pending;
    });
    if (ids.length !== selected.length) { set({ error: "Seçilen sonuçlar değişti; yeniden seçin.", selected: [] }); return; }
    const capturedRoot = root, token = generation;
    mutationRevision += 1;
    set({ busy: true, error: "" });
    try {
      const { candidate } = await bridge.call("candidate.prepare", { runIds: ids, verify: true });
      if (root !== capturedRoot || token !== generation) return;
      if (candidate.projectRoot !== capturedRoot) throw new Error("Aday başka bir projeye ait.");
      mutationRevision += 1;
      set((s) => ({ candidates: [candidate, ...s.candidates.filter((c) => c.candidateId !== candidate.candidateId)].slice(0, 32), selected: [] }));
    } catch (error) {
      if (root === capturedRoot && token === generation) set({ error: error instanceof Error ? error.message : "Aday oluşturulamadı." });
    } finally { if (root === capturedRoot && token === generation) set({ busy: false }); }
  },
  apply: async (id) => mutate(id, false),
  rollback: async (id) => mutate(id, true),
}));

async function refreshCandidateSource(
  paths: string[],
  before: Array<{ tab: Tab; content: string; draft: string; tooLarge: boolean }>,
  capturedRoot: string,
  token: number,
) {
  const [{ useWorkspace }, { useScm }] = await Promise.all([
    import("@/state/workspace"), import("@/state/scm"),
  ]);
  const current = () => root === capturedRoot && generation === token && useWorkspace.getState().root === capturedRoot;
  for (const rel of paths) {
    if (!current()) return;
    const original = before.find((entry) => entry.tab.rel === rel);
    if (!original) continue;
    const stillUnchanged = () => {
      const tab = useEditor.getState().tabs.find((item) => item.rel === rel);
      return tab === original.tab && !tab.dirty && tab.content === original.content && tab.draft === original.draft && tab.tooLarge === original.tooLarge;
    };
    if (!stillUnchanged()) continue;
    try {
      const { content, tooLarge } = await bridge.call("fs.readFile", { rel });
      if (!current() || !stillUnchanged()) continue;
      useEditor.setState((state) => ({ tabs: state.tabs.map((tab) => tab === original.tab && tab.rel === rel && !tab.dirty
        ? { ...tab, content, draft: content, tooLarge: !!tooLarge } : tab) }));
    } catch (error) {
      if (!current() || !stillUnchanged()) continue;
      if (typeof error === "object" && error !== null && "code" in error && error.code === "not_found") {
        useEditor.getState().closeDeleted(rel);
      }
    }
  }
  if (!current()) return;
  const workspace = useWorkspace.getState();
  const dirs = new Set(Object.keys(workspace.children));
  for (const path of paths) {
    const slash = path.lastIndexOf("/");
    dirs.add(slash < 0 ? "" : path.slice(0, slash));
  }
  await Promise.all([...dirs].map((dir) => workspace.loadDir(dir, capturedRoot)));
  if (!current()) return;
  await useScm.getState().refresh(capturedRoot);
}

async function mutate(id: string, rollback: boolean) {
  const state = useCandidate.getState();
  const candidate = state.candidates.find((c) => c.candidateId === id);
  if (!root || state.busy || !candidate || candidate.projectRoot !== root) return;
  if (rollback ? candidate.state !== "applied" : candidate.state !== "prepared" || candidate.verification.status !== "pass") return;
  const capturedRoot = root, token = generation;
  const dirty = useEditor.getState().tabs.some((tab) => tab.dirty && candidate.changedPaths.includes(tab.rel));
  if (dirty) { useCandidate.setState({ error: "Önce ilgili kaydedilmemiş sekmeleri kaydedin." }); return; }
  useCandidate.setState({ busy: true, error: "" });
  try {
    const accepted = await confirmDialog({
      title: rollback ? "Birleşik adayı geri al" : "Doğrulanmış adayı uygula",
      message: `${candidate.changedPaths.length} dosya ${rollback ? "checkpoint durumuna dönecek" : "projenize yazılacak; önce checkpoint alınacak"}.`,
      okLabel: rollback ? "Geri Al" : "Uygula", danger: rollback,
    });
    if (!accepted || root !== capturedRoot || token !== generation) return;
    if (useEditor.getState().tabs.some((tab) => tab.dirty && candidate.changedPaths.includes(tab.rel))) throw new Error("Kaydedilmemiş değişiklikler var.");
    const beforeMutation = useEditor.getState().tabs
      .filter((tab) => candidate.changedPaths.includes(tab.rel) && !tab.dirty)
      .map((tab) => ({ tab, content: tab.content, draft: tab.draft, tooLarge: tab.tooLarge }));
    const paths = rollback
      ? (await bridge.call("candidate.rollback", { candidateId: id })).restored
      : (await bridge.call("candidate.apply", { candidateId: id })).applied;
    if (root !== capturedRoot || token !== generation) return;
    await refreshCandidateSource(paths, beforeMutation, capturedRoot, token);
    if (root !== capturedRoot || token !== generation) return;
    mutationRevision += 1;
    await useCandidate.getState().refresh();
  } catch (error) {
    if (root === capturedRoot && token === generation) {
      useCandidate.setState({ error: error instanceof Error ? error.message : "Entegrasyon başarısız." });
      await useCandidate.getState().refresh();
    }
  } finally { if (root === capturedRoot && token === generation) useCandidate.setState({ busy: false }); }
}

useWorkspace.subscribe((state) => {
  if (state.root !== root) {
    root = state.root;
    generation += 1;
    useCandidate.setState({ candidates: [], selected: [], busy: false, error: "" });
  }
});
