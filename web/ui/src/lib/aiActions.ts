/* aiActions.ts — R4 Command Center: Ctrl+K ve output akışını AI-native çalışma
   turuna bağlayan eylemler. Hepsi mevcut run lifecycle'ına (setTask → start → plan
   → öneri → inceleme → apply → checkpoint) düşer; ayrı bir sohbet kanalı açılmaz.
   Bağlam görev metnine gömülür (run.start yalnız {task, routing} alır — backend
   sözleşmesi değişmez). */

import { useRun } from "@/state/run";
import { useUi } from "@/state/ui";
import { useEditor } from "@/state/editor";
import { useWorkspace } from "@/state/workspace";
import { toast } from "@/components/toasts/toasts";

const RAW_TAIL = 4000; // ekibe taşınan çıktı kuyruğunun üst sınırı (token bütçesi)

/** ANSI/VT kaçış dizilerini at — ham exec tamponu ekibe düz metin gider */
function stripAnsi(s: string): string {
  // eslint-disable-next-line no-control-regex
  return s.replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "");
}

export interface SelectionContext {
  rel: string;
  code: string;
  startLine: number;
  endLine: number;
}

/** Aktif Monaco editöründeki boş olmayan seçim; requireFocus true ise yalnız editör
    odaktayken döner (Ctrl+K'nın seçim/komut ayrımı için). */
async function selectionContext(requireFocus = false): Promise<SelectionContext | null> {
  const { getActiveCodeEditor } = await import("@/components/editor/Editor");
  const ed = getActiveCodeEditor();
  if (!ed) return null;
  if (requireFocus && !ed.hasTextFocus()) return null;
  const sel = ed.getSelection();
  const model = ed.getModel();
  if (!sel || !model || sel.isEmpty()) return null;
  const code = model.getValueInRange(sel);
  if (!code.trim()) return null;
  const rel = useEditor.getState().activeRel ?? model.uri.path.replace(/^\//, "");
  return { rel, code, startLine: sel.startLineNumber, endLine: sel.endLineNumber };
}

/** keymap: seçim varken Ctrl+K inline edit, yoksa command center */
export async function hasActiveSelection(): Promise<boolean> {
  return (await selectionContext(true)) !== null;
}

/** Ortak: görevi kur, AI panelini aç, çalıştır. run.start() running/ready korumasını
    ve boş görev toast'ını kendisi yapar. */
async function startTeamRun(task: string) {
  const run = useRun.getState();
  useUi.getState().showAiPanel();
  run.setTask(task);
  await run.start();
}

/** "Görev ver" — AI panelini aç + composer'a odaklan */
export function giveTask() {
  useUi.getState().focusComposer();
}

/** Ctrl+K (seçim varken) / "Seçili kodu düzenle" — talimat iste, ekibe gönder */
export async function inlineEditSelection() {
  const ctx = await selectionContext();
  if (!ctx) {
    toast.info("Önce düzenlenecek kodu seçin.");
    return;
  }
  const { promptDialog } = await import("@/components/dialogs/dialogs");
  const instruction = await promptDialog({
    title: "Seçili kodu düzenle",
    message: `${ctx.rel} · ${ctx.startLine}-${ctx.endLine}. satır`,
    okLabel: "Ekibe gönder",
    placeholder: "ör. bu döngüyü list comprehension yap",
  });
  if (!instruction?.trim()) return;
  const task = [
    `\`${ctx.rel}\` dosyasının ${ctx.startLine}-${ctx.endLine}. satırlarındaki şu kodu düzenle:`,
    "",
    "```",
    ctx.code,
    "```",
    "",
    `İstenen değişiklik: ${instruction.trim()}`,
  ].join("\n");
  await startTeamRun(task);
}

/** "Seçili kodu açıkla" — diff üretmeden ekipten açıklama iste */
export async function explainSelection() {
  const ctx = await selectionContext();
  if (!ctx) {
    toast.info("Önce açıklanacak kodu seçin.");
    return;
  }
  const task = [
    `\`${ctx.rel}\` dosyasının ${ctx.startLine}-${ctx.endLine}. satırlarındaki şu kodu açıkla; ne yaptığını ve olası sorunları özetle:`,
    "",
    "```",
    ctx.code,
    "```",
  ].join("\n");
  await startTeamRun(task);
}

/** "Output hatasını ekibe gönder" — F5/çalıştırma çıktısını görev bağlamı yapar */
export async function sendOutputErrorToTeam() {
  const { useExec } = await import("@/state/exec");
  const ex = useExec.getState();
  const raw = stripAnsi(ex.raw).trim();
  if (!raw) {
    toast.info("Aktarılacak çıktı yok. Önce bir şey çalıştırın.");
    return;
  }
  const tail = raw.length > RAW_TAIL ? "…\n" + raw.slice(-RAW_TAIL) : raw;
  const state = ex.exitCode === null ? "koşu sürüyor" : `çıkış kodu ${ex.exitCode}`;
  const task = [
    `Aşağıdaki çalıştırma çıktısında bir sorun var (komut: \`${ex.command || "?"}\`, ${state}).`,
    "Kök nedeni bul ve gereken dosyaları düzelt:",
    "",
    "```",
    tail,
    "```",
  ].join("\n");
  await startTeamRun(task);
}

/** "Değişiklikleri incele" — AI panelini aç + ilk diff'i merkezde göster */
export function reviewChanges() {
  const run = useRun.getState();
  if (!run.diffs.length && !run.proposals.length) {
    toast.info("İncelenecek değişiklik yok.");
    return;
  }
  useUi.getState().showAiPanel();
  const first = run.diffs[0]?.path ?? run.proposals[0]?.path;
  if (first) void import("@/state/editor").then(({ useEditor }) => useEditor.getState().openDiff(first));
}

/** "Checkpoint'e dön" — son apply checkpoint'ini onaylı restore et */
export function restoreLastCheckpoint() {
  void useRun.getState().restoreCheckpoint();
}

/** "Testleri çalıştır" — komut hatırlanır (localStorage), ÇIKTI'ya akar */
export async function runTests() {
  if (!useWorkspace.getState().root) {
    toast.info("Önce bir proje klasörü aç.");
    return;
  }
  const remembered = localStorage.getItem("magent.testCommand") ?? "";
  const { promptDialog } = await import("@/components/dialogs/dialogs");
  const next = await promptDialog({
    title: "Testleri çalıştır",
    message: "Bu komut proje kökünde koşar ve ÇIKTI paneline akar.",
    initial: remembered,
    okLabel: "Çalıştır",
    placeholder: "ör. pytest -q · npm test",
  });
  if (!next?.trim()) return;
  const cmd = next.trim();
  localStorage.setItem("magent.testCommand", cmd);
  const { useExec } = await import("@/state/exec");
  void useExec.getState().run(null, cmd);
}
