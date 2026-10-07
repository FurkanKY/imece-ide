import { useEffect, useRef, useState } from "react";
import { create } from "zustand";
import { bridge, type PeerProposalPreview } from "@/bridge";
import { useParticipant } from "@/state/participant";
import { useWorkspace } from "@/state/workspace";
import { projectEpoch } from "@/state/projectEpoch";
import { useRun } from "@/state/run";
import { Button } from "@/components/ui";

const useSharing = create<{ preview: PeerProposalPreview | null; error: string }>(() => ({ preview: null, error: "" }));
useWorkspace.subscribe((next, previous) => {
  if (next.root !== previous.root) useSharing.setState({ preview: null, error: "" });
});
useParticipant.subscribe((next, previous) => {
  if (next.session?.peerHandle !== previous.session?.peerHandle) useSharing.setState({ preview: null, error: "" });
});
const input = "min-w-0 rounded-[var(--r-sm)] border border-border-w bg-field px-2 py-1.5 text-text";

export function PeerDelivery() {
  const root = useWorkspace((s) => s.root);
  const session = useParticipant((s) => s.session);
  const busy = useParticipant((s) => s.busy);
  const runs = useRun((s) => s.runs);
  const { preview, error } = useSharing();
  const [taskId, setTaskId] = useState("");
  const [sourceRunId, setSourceRunId] = useState("");
  const [paths, setPaths] = useState("");
  const [consent, setConsent] = useState(false);
  const [scopeConsent, setScopeConsent] = useState(false);
  const [fetchId, setFetchId] = useState("");
  const [fetched, setFetched] = useState("");
  const flight = useRef(false);
  const owned = Object.values(runs).filter((run) => run.root === root && !run.readOnly && run.runStage === "ready" && !run.pending && !run.uncertain);
  useEffect(() => { setTaskId(""); setSourceRunId(""); setPaths(""); setConsent(false); setScopeConsent(false); setFetchId(""); setFetched(""); }, [root, session?.peerHandle]);
  useEffect(() => { setConsent(false); setScopeConsent(false); }, [preview?.ticketId]);
  if (!root || session?.projectRoot !== root || session.state !== "active") return null;
  const tasks = (session.tasks ?? []).filter((task) => task.owner === session.memberId && task.status !== "done");
  const act = async (operation: "preview" | "publish" | "reconcile" | "discard" | "fetch") => {
    if (flight.current || !session || !root) return;
    const token = useParticipant.getState().begin();
    if (token === null) return;
    flight.current = true;
    const epoch = projectEpoch(), handle = session.peerHandle, capturedRoot = root;
    const current = () => projectEpoch() === epoch && useWorkspace.getState().root === capturedRoot && useParticipant.getState().session?.peerHandle === handle;
    useSharing.setState({ error: "" });
    try {
      if (operation === "preview") {
        const selected = [...new Set(paths.split(/\r?\n/).map((path) => path.trim()).filter(Boolean))];
        const result = await bridge.call("collab.peer.previewProposal", { peerHandle: handle, taskId, paths: selected, sourceRunId: sourceRunId || undefined });
        if (current()) useSharing.setState({ preview: result });
      } else if (operation === "publish" && preview && consent) {
        const result = await bridge.call("collab.peer.publishProposal", { peerHandle: handle, ticketId: preview.ticketId, confirm: true, allowOutOfScope: scopeConsent });
        if (current()) useSharing.setState({ preview: result });
      } else if (operation === "reconcile" && preview) {
        const result = await bridge.call("collab.peer.reconcileProposal", { peerHandle: handle, ticketId: preview.ticketId });
        if (current()) useSharing.setState({ preview: result });
      } else if (operation === "discard" && preview) {
        await bridge.call("collab.peer.discardProposal", { peerHandle: handle, ticketId: preview.ticketId });
        if (current()) useSharing.setState({ preview: null });
      } else if (operation === "fetch") {
        const result = await bridge.call("collab.peer.fetchProposal", { peerHandle: handle, proposalId: fetchId.trim() });
        if (current()) setFetched(`${result.proposalId} · ${result.owner} · ${result.fileCount} dosya · SHA-256 ${result.artifactSha256}`);
      }
    } catch (failure) {
      if (current()) {
        const uncertain = typeof failure === "object" && failure !== null && "code" in failure && failure.code === "peer_publish_uncertain";
        useSharing.setState({ error: failure instanceof Error ? failure.message : "Paylaşım işlemi tamamlanamadı.",
          ...(uncertain && preview ? { preview: { ...preview, state: "unknown" as const } } : {}) });
      }
    } finally { flight.current = false; useParticipant.getState().finish(token); }
  };
  return <section className="mt-3 grid gap-2 border-t border-border-w pt-2" aria-label="Seçili kod paylaşımı">
    <h3 className="text-text">Seçili kodu öneri olarak paylaş</h3>
    <p className="text-muted">Yalnız seçtiğiniz dosyaların ortak temele göre değişiklikleri aktarılır. İçerik olduğu gibi paylaşılır; gizli bilgiler otomatik temizlenmez. Source'a yazılmaz.</p>
    {!preview && <fieldset disabled={busy} className="grid min-w-0 gap-2">
      <label className="grid gap-1">Size atanmış ortak görev<select className={input} value={taskId} onChange={(e) => setTaskId(e.target.value)}><option value="">Görev seçin</option>{tasks.map((task) => <option key={task.id} value={task.id}>{task.id} · {task.goal}</option>)}</select></label>
      <label className="grid gap-1">Paylaşılacak yerel kaynak<select className={input} value={sourceRunId} onChange={(e) => setSourceRunId(e.target.value)}><option value="">Mevcut Source değişiklikleri · elle seçilen dosyalar</option>{owned.map((run) => <option key={run.runId} value={run.runId}>{run.task} · izole sonuç</option>)}</select></label>
      <label className="grid gap-1">Açıkça seçilen göreli dosyalar · satır başına bir yol<textarea className={input} maxLength={32768} value={paths} onChange={(e) => setPaths(e.target.value)} placeholder="src/example.py" /></label>
      <Button size="sm" disabled={busy || !taskId || !paths.trim()} onClick={() => void act("preview")}>Seçili öneriyi önizle</Button>
    </fieldset>}
    {preview && <div className="grid min-w-0 gap-2" data-proposal-id={preview.proposalId}>
      <p className="break-all">{preview.proposalId} · {preview.fileCount} dosya · {preview.state}<br />SHA-256: {preview.artifactSha256}</p>
      <ul>{preview.paths.map((path) => <li className="break-all" key={path}>{path}</li>)}</ul>
      {preview.state === "preview" && <>
        <label className="flex items-start gap-2"><input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />Bu dosyaların içeriğini seçtiğim ekip oturumuyla olduğu gibi paylaşmayı onaylıyorum.</label>
        {preview.outOfScopePaths.length > 0 && <label className="flex items-start gap-2 text-warn"><input type="checkbox" checked={scopeConsent} onChange={(e) => setScopeConsent(e.target.checked)} />Kapsam dışı yolları ayrıca onaylıyorum: {preview.outOfScopePaths.join(", ")}</label>}
        <Button size="sm" disabled={busy || !consent || preview.outOfScopePaths.length > 0 && !scopeConsent} onClick={() => void act("publish")}>Öneriyi açıkça paylaş</Button>
      </>}
      {preview.state === "unknown" && <><p className="text-warn">Paylaşım sonucu belirsiz. Yeni kimlikle göndermeyin; aynı öneri kimliğinin içeriğini doğrulayın.</p><Button size="sm" disabled={busy} onClick={() => void act("reconcile")}>Aynı öneri kimliğini uzlaştır</Button></>}
      {preview.state !== "unknown" && <Button size="sm" variant="secondary" disabled={busy} onClick={() => void act("discard")}>{preview.state === "published" ? "Yeni açık dosya seçimine dön" : "Önizlemeyi iptal et"}</Button>}
    </div>}
    <label className="grid gap-1">Alınacak tek öneri kimliği<input className={input} maxLength={128} value={fetchId} onChange={(e) => setFetchId(e.target.value)} /></label>
    <Button size="sm" variant="secondary" disabled={busy || !fetchId.trim()} onClick={() => void act("fetch")}>Yalnız bu önerinin makbuzunu al</Button>
    {fetched && <p className="break-all" role="status">{fetched}</p>}
    {error && <p className="break-words text-err" role="alert">{error}</p>}
  </section>;
}
