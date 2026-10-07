import { useEffect } from "react";
import { useCandidate } from "@/state/candidate";
import { useRun } from "@/state/run";
import { useWorkspace } from "@/state/workspace";
import { Button } from "@/components/ui";

export function CandidatePanel({ sharedOnly = false }: { sharedOnly?: boolean }) {
  const root = useWorkspace((s) => s.root);
  const runs = useRun((s) => s.runs);
  const { candidates, selected, busy, error, toggle, refresh, prepare, apply, rollback } = useCandidate();
  const eligible = Object.values(runs).filter((r) => r.root === root && !r.readOnly
    && r.runStage === "ready" && !r.pending && !r.uncertain);
  const visible = sharedOnly ? candidates.filter((candidate) => !!candidate.sharedProvenance) : candidates;
  const sharedCount = candidates.filter((candidate) => !!candidate.sharedProvenance).length;
  const nativeCount = candidates.length - sharedCount;
  useEffect(() => { void refresh(); }, [root, refresh]);
  return <details className="border-t border-border-w px-3 py-2" aria-label="Birleşik adaylar">
    <summary className="cursor-pointer text-text2" style={{ fontSize: "var(--t-label)" }}>{sharedOnly ? `Ortak adayları uygula / geri al · ${sharedCount}` : `Sonuçları birleştir · yerel ${nativeCount} / ortak ${sharedCount}`}</summary>
    <p className="mt-2 text-muted" style={{ fontSize: "var(--t-caption)" }}>Sonuçlar ayrı bir dizinde birleştirilir. Doğrulama proje komutlarını çalıştırır; projenize ancak açık onayla yazılır.</p>
    {!sharedOnly && <fieldset className="mt-2 space-y-2" disabled={busy}>
      <legend className="sr-only">Birleştirilecek sonuçları seç</legend>
      {eligible.map((run) => <label key={run.runId} className="flex items-start gap-2 text-text2" style={{ fontSize: "var(--t-caption)" }}>
        <input type="checkbox" checked={selected.includes(run.runId)} onChange={() => toggle(run.runId)} aria-label={`Birleştirmek için seç: ${run.task}`} />
        <span className="min-w-0 break-words">{run.task}</span>
      </label>)}
      {eligible.length === 0 && <p className="text-muted" style={{ fontSize: "var(--t-caption)" }}>Önce bu oturumda inceleme bekleyen bir sonuç oluşturun.</p>}
    </fieldset>}
    <div className="my-2 flex flex-wrap gap-2">
      {!sharedOnly && <Button size="sm" variant="secondary" disabled={busy || !selected.length} loading={busy} onClick={() => void prepare()}>Birleştir ve doğrula</Button>}
      <Button size="sm" variant="ghost" disabled={busy} onClick={() => void refresh()}>Adayları yenile</Button>
    </div>
    {error && <p role="alert" className="mb-2 break-words text-err" style={{ fontSize: "var(--t-caption)" }}>{error}</p>}
    <ul className="space-y-2">
      {visible.map((candidate) => <li key={candidate.candidateId} className="border-t border-border-w pt-2" data-candidate-id={candidate.candidateId}>
        <p className="text-text2" style={{ fontSize: "var(--t-caption)" }}>{candidate.sharedProvenance ? `${candidate.selectedProposals?.length ?? candidate.sharedProvenance.proposalIds.length} ortak teklif` : `${candidate.selected.length} yerel sonuç`} · {candidate.changedPaths.length} dosya · {candidate.state === "applied" ? "Uygulandı" : candidate.state === "rolled_back" ? "Geri alındı" : `Doğrulama: ${candidate.verification.status}`}</p>
        <details className="my-1 text-muted" style={{ fontSize: "var(--t-caption)" }}><summary>Dosyaları ve aday konumunu göster</summary>
          <p className="break-all">{candidate.candidateDir}</p><ul>{candidate.changedPaths.map((path) => <li key={path} className="break-all">{path}</li>)}</ul>
        </details>
        {candidate.state === "prepared" && <Button variant="secondary" size="sm" disabled={busy || candidate.verification.status !== "pass"} onClick={() => void apply(candidate.candidateId)}>Doğrulanmış adayı uygula</Button>}
        {candidate.state === "applied" && <Button variant="secondary" size="sm" disabled={busy} onClick={() => void rollback(candidate.candidateId)}>Birleşik adayı geri al</Button>}
      </li>)}
    </ul>
  </details>;
}
