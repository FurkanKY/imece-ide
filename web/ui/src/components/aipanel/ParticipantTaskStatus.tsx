import { useEffect, useRef, useState } from "react";
import { bridge } from "@/bridge";
import { BridgeError, ParticipantTaskStatusPreview } from "@/bridge/protocol";
import { Button } from "@/components/ui/Button";

type Props = { root: string; runId: string; sessionId: string; memberId: string; taskId: string };
const targets = ["queued", "running", "waiting"] as const;

export function ParticipantTaskStatus({ root, runId, sessionId, memberId, taskId }: Props) {
  const [target, setTarget] = useState<(typeof targets)[number]>("waiting");
  const [preview, setPreview] = useState<ParticipantTaskStatusPreview | null>(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const flight = useRef(false);
  const generation = useRef(0);
  const previewRef = useRef<ParticipantTaskStatusPreview | null>(null);
  const identity = `${root}|${runId}|${sessionId}|${memberId}|${taskId}`;

  const discard = (ticket: ParticipantTaskStatusPreview | null) => {
    if (ticket) void bridge.call("collab.taskStatus.discard", { runId: ticket.runId, ticketId: ticket.ticketId }).catch(() => undefined);
  };
  const invalidate = () => {
    generation.current++;
    discard(previewRef.current); previewRef.current = null; setPreview(null);
    setConsent(false);
    setMessage("");
  };

  useEffect(() => {
    invalidate();
    return () => { generation.current++; discard(previewRef.current); previewRef.current = null; };
    // Identity changes intentionally clear any reviewed ticket.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity]);

  const getPreview = async () => {
    if (flight.current) return;
    flight.current = true; setBusy(true); setMessage("");
    const token = ++generation.current;
    try {
      const next = await bridge.call("collab.taskStatus.preview", { runId, targetStatus: target });
      if (generation.current !== token || next.projectRoot !== root || next.runId !== runId || next.sessionId !== sessionId || next.memberId !== memberId || next.taskId !== taskId || next.targetStatus !== target) {
        discard(next); return;
      }
      previewRef.current = next; setPreview(next); setConsent(false);
    } catch (error) {
      if (generation.current === token) setMessage(error instanceof Error ? error.message : "Önizleme alınamadı.");
    } finally { flight.current = false; setBusy(false); }
  };

  const confirm = async () => {
    if (!preview || !consent || flight.current) return;
    const ticket = preview;
    flight.current = true; setBusy(true); previewRef.current = null; setPreview(null); setConsent(false); setMessage("");
    const token = ++generation.current;
    try {
      const receipt = await bridge.call("collab.taskStatus.confirm", { runId, ticketId: ticket.ticketId, confirm: true });
      if (generation.current !== token) return;
      if (!receipt || receipt.taskId !== ticket.taskId || receipt.status !== ticket.targetStatus ||
          typeof receipt.revision !== "string" || !receipt.revision) {
        setMessage("Yazım yapılmış OLABİLİR. Yanıt doğrulanamadı; yeni bir önizlemeyle durumu uzlaştırın. Otomatik tekrar yok.");
        return;
      }
      setMessage(`Görev durumu kaydedildi: ${receipt.status} · revizyon ${receipt.revision}`);
    } catch (error) {
      if (generation.current !== token) return;
      if (error instanceof BridgeError && error.code === "collab_task_outcome_unknown") {
        setMessage("Yazım yapılmış OLABİLİR. Otomatik yeniden deneme veya okuma yapılmadı; yeni bir önizlemeyle durumu uzlaştırın.");
      } else setMessage(error instanceof Error ? error.message : "Görev durumu onaylanamadı.");
    } finally {
      // A bridge-level refusal can happen before the host spends the ticket.
      // Discard is idempotent and never retries or reconciles the write.
      discard(ticket);
      flight.current = false; setBusy(false);
    }
  };

  return <section className="space-y-2 rounded-[var(--r-sm)] border border-border-w p-2" aria-label="Görev durumum">
    <strong>Görev durumum</strong>
    <p className="break-words text-muted">Üye: {memberId} · görev: {taskId}</p>
    <label className="block text-muted">Yeni durum
      <select className="mt-1 w-full rounded-[var(--r-sm)] border border-border-w2 bg-field px-2 py-1.5 text-text" value={target} disabled={busy} onChange={(event) => { invalidate(); setTarget(event.target.value as (typeof targets)[number]); }}>
        {targets.map((value) => <option key={value} value={value}>{value}</option>)}
      </select>
    </label>
    <Button variant="secondary" disabled={busy || !!preview} onClick={() => void getPreview()}>{busy ? "Bekleyin…" : "Durum değişikliğini önizle"}</Button>
    {preview && <div className="space-y-2 break-words rounded-[var(--r-sm)] bg-panel p-2" aria-live="polite">
      <p>İncelenen değişiklik: <strong>{preview.fromStatus} → {preview.targetStatus}</strong></p>
      <p>Beklenen revizyon: {preview.expectedRevision}</p>
      <p className="text-muted">Önizlenen bağlamın kabul edilen bağlamdan farkı: {preview.freshContextDiffersFromAccepted === null ? "kabul edilmiş bağlam bilinmiyor" : preview.freshContextDiffersFromAccepted ? "farklı" : "aynı"}. Bu yalnızca bilgilendirmedir; bağlam onayı değildir.</p>
      <label className="flex items-start gap-2"><input type="checkbox" checked={consent} disabled={busy} onChange={(event) => setConsent(event.target.checked)} />Bu üye ve görev için önizlenen durum değişikliğini açıkça onaylıyorum.</label>
      <Button variant="primary" disabled={busy || !consent} onClick={() => void confirm()}>Durum değişikliğini uygula</Button>
      <Button variant="secondary" disabled={busy} onClick={invalidate}>Önizlemeyi bırak</Button>
    </div>}
    {message && <p role="status" aria-live="polite" className="break-words text-muted">{message}</p>}
  </section>;
}
