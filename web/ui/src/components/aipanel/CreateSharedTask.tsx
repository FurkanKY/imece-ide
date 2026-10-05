import { useEffect, useRef, useState } from "react";
import { ProductBoard } from "@/bridge";
import { Button } from "@/components/ui";

const input = "min-w-0 w-full rounded-[var(--r-sm)] border border-border-w bg-field px-2 py-1.5 text-text outline-none focus-visible:border-accent";
type TaskDraft = { id: string; owner: string; goal: string; scopes: string };
const empty = (): TaskDraft => ({ id: "", owner: "", goal: "", scopes: "" });

export function CreateSharedTask({ board, fresh, busy, onSubmit }: {
  board: ProductBoard; fresh: boolean; busy: boolean;
  onSubmit: (revision: string, task: { id: string; owner: string; goal: string; scopes: string[] }) => Promise<boolean>;
}) {
  const identity = `${board.projectRoot}|${board.sessionId}|${board.epoch}`;
  const [draft, setDraft] = useState<TaskDraft>(empty);
  const [revision, setRevision] = useState(board.revision);
  const [confirmed, setConfirmed] = useState(false);
  const [open, setOpen] = useState(false);
  const [draftActive, setDraftActive] = useState(false);
  const [reviewedContext, setReviewedContext] = useState(board.context);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState("");
  const mounted = useRef(false);
  const identityGeneration = useRef(0);
  const submittingRef = useRef(false);
  const previousIdentity = useRef(identity);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; identityGeneration.current++; submittingRef.current = false; }; }, []);
  useEffect(() => {
    if (previousIdentity.current === identity) return;
    previousIdentity.current = identity; identityGeneration.current++; submittingRef.current = false;
    setSubmitting(false); setDraft(empty()); setDraftActive(false); setRevision(board.revision);
    setReviewedContext(board.context); setConfirmed(false); setOpen(false); setLocalError("");
  }, [identity, board.revision, board.context]);
  useEffect(() => {
    setConfirmed(false);
    if (!draftActive) { setRevision(board.revision); setReviewedContext(board.context); }
  }, [board.revision, fresh, identity, draftActive]);
  const scopes = draft.scopes.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const known = board.tasks.some((task) => task.id === draft.id.trim());
  const stale = revision !== board.revision;
  const validId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(draft.id.trim());
  const valid = validId && board.memberIds.includes(draft.owner) && !!draft.goal.trim() && draft.goal.length <= 4000 && scopes.length <= 32 && scopes.every((scope) => scope.length <= 512);
  const edit = (field: keyof TaskDraft, value: string) => { setDraftActive(true); setDraft((old) => ({ ...old, [field]: value })); setConfirmed(false); setLocalError(""); };
  const reviewCurrent = () => { setRevision(board.revision); setReviewedContext(board.context); setConfirmed(false); setLocalError(""); };
  const submit = async () => {
    if (submittingRef.current || !valid || !confirmed || stale || !fresh || busy || board.state !== "running") return;
    submittingRef.current = true; setSubmitting(true); setConfirmed(false); setLocalError("");
    const generation = identityGeneration.current, submittedIdentity = identity;
    try {
      const created = await onSubmit(revision, { id: draft.id.trim(), owner: draft.owner, goal: draft.goal.trim(), scopes });
      if (!mounted.current || generation !== identityGeneration.current || submittedIdentity !== previousIdentity.current) return;
      if (created) { setDraft(empty()); setDraftActive(false); setRevision(board.revision); setReviewedContext(board.context); setOpen(false); }
      else setLocalError("Görev oluşturma onaylanamadı. Taslak korundu; panoyu ve görev kimliğini kontrol edip ayrıca onaylayın.");
    } catch {
      if (mounted.current && generation === identityGeneration.current && submittedIdentity === previousIdentity.current)
        setLocalError("Görev oluşturma onaylanamadı. Taslak korundu; panoyu ve görev kimliğini kontrol edip ayrıca onaylayın.");
    } finally {
      if (mounted.current && generation === identityGeneration.current && submittedIdentity === previousIdentity.current) {
        submittingRef.current = false; setSubmitting(false); setConfirmed(false);
      }
    }
  };
  const inputDisabled = busy || submitting || !fresh || board.state !== "running";
  return <section className="grid min-w-0 gap-2 border-b border-border-w py-3">
    <div className="flex flex-wrap items-center justify-between gap-2"><b>Yeni görev · yalnızca oluştur</b>{!open && <Button size="sm" disabled={!fresh || busy || submitting || board.state !== "running"} onClick={() => { setRevision(board.revision); setReviewedContext(board.context); setConfirmed(false); setOpen(true); }}>Yeni görev</Button>}</div>
    {open && <fieldset className="grid min-w-0 gap-2" disabled={inputDisabled}>
      <legend className="text-faint" style={{ fontSize: "var(--t-caption)" }}>Atandığında queued başlar; bu işlem çalıştırma veya önizleme başlatmaz.</legend>
      <div className="grid min-w-0 gap-1 border border-border-w p-2 text-text2">
        <b className="break-all">İncelenen yayınlanmış bağlam · rev {revision}</b>
        <span className="break-all">Ürün: {board.sessionId} · sürüm {board.targetVersion}</span>
        <p className="break-words [overflow-wrap:anywhere]">{reviewedContext.goal || "Ürün hedefi belirtilmemiş."}</p>
        <details className="min-w-0"><summary className="cursor-pointer">Kararlar ({reviewedContext.decisions.length}) ve arayüzler ({Object.keys(reviewedContext.interfaces).length})</summary>
          <p className="my-1 text-muted">Bu yayınlanmış kaynak, üstteki düzenleme taslağından bağımsızdır.</p>
          {reviewedContext.decisions.map((decision, index) => <p key={index} className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">Karar: {decision}</p>)}
          {Object.entries(reviewedContext.interfaces).map(([name, value]) => <p key={name} className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]"><b>{name}</b>: {value}</p>)}
        </details>
      </div>
      <label className="grid gap-1">Görev kimliği<input aria-label="Yeni görev kimliği" className={input} maxLength={128} value={draft.id} onChange={(event) => edit("id", event.target.value)} /></label>
      <label className="grid gap-1">Atanmış üye<select aria-label="Yeni görev atanmış üye" className={input} value={draft.owner} onChange={(event) => edit("owner", event.target.value)}><option value="">Üye seçin</option>{board.memberIds.map((member) => <option key={member} value={member}>{member}</option>)}</select></label>
      <label className="grid gap-1">Görev amacı<textarea aria-label="Yeni görev amacı" className={input} maxLength={4000} value={draft.goal} onChange={(event) => edit("goal", event.target.value)} /></label>
      <label className="grid gap-1">Kapsam yolları · satır başına yol (öneri, kilit değildir)<textarea aria-label="Yeni görev kapsam yolları, satır başına" className={input} value={draft.scopes} onChange={(event) => edit("scopes", event.target.value)} /></label>
      {known && <p role="alert" className="text-warn">Bu görev kimliği zaten var; mevcut görev değiştirilemez.</p>}
      {stale && <div className="grid gap-1 text-warn"><p>Taslak rev {revision} için incelenmiş; pano rev {board.revision}. Taslak korunuyor ve otomatik yeniden tabanlanmıyor.</p><Button size="sm" disabled={!fresh || busy || submitting} onClick={reviewCurrent}>Görev taslağını güncel revizyonla yeniden incele</Button></div>}
      {localError && <p role="alert" className="text-err">{localError}</p>}
      <label className="flex min-w-0 items-start gap-2"><input type="checkbox" checked={confirmed && !stale} disabled={!fresh || busy || stale || !valid || known} onChange={(event) => setConfirmed(event.target.checked)} /><span>Görevi {revision} revizyonundaki görünür ortak bağlama, seçilen üyeye queued olarak eklemeyi onaylıyorum.</span></label>
      <div className="flex flex-wrap gap-2"><Button size="sm" variant="primary" loading={busy || submitting} disabled={!fresh || stale || !valid || known || !confirmed || busy || submitting || board.state !== "running"} onClick={() => void submit()}>Görevi oluştur</Button><Button size="sm" disabled={busy || submitting} onClick={() => { setDraft(empty()); setDraftActive(false); setRevision(board.revision); setReviewedContext(board.context); setConfirmed(false); setOpen(false); setLocalError(""); }}>İptal</Button></div>
    </fieldset>}
  </section>;
}
