import { useEffect, useState } from "react";
import { useCollaboration } from "@/state/collaboration";
import { useRun } from "@/state/run";
import { useWorkspace } from "@/state/workspace";
import { Button } from "@/components/ui/Button";
import { collaborationNeedsCursorReset, collaborationNeedsRecovery } from "@/state/collaboration";
import { ParticipantTaskStatus } from "./ParticipantTaskStatus";

const statusLabels: Record<string, string> = {
  unavailable: "Ortak oturum durumu doğrulanamadı; son bilinen bilgiler korunuyor",
  active: "Etkin", waiting_user: "Kullanıcı bekleniyor", closed: "Son execution aboneliği kapalı; takip isteğinde yeniden bağlanır",
  inactive: "Son execution aboneliği kapalı; takip isteğinde yeniden bağlanır", stopped: "Son execution aboneliği kapalı; takip isteğinde yeniden bağlanır",
  resnapshot_required: "Yeni anlık görüntü ve açık kurtarma onayı gerekli", access_denied: "Erişim reddedildi; kimlik bilgilerini yeniden doğrulayın",
  protocol_error: "Protokol hatası; yeni koşu ve açık kurtarma onayı gerekli", server_error: "Sunucu hatası; yeni koşu ve açık kurtarma onayı gerekli",
  cleanup_failed: "Temizlik başarısız; yeni koşu ve açık kurtarma onayı gerekli", reset_pending: "Bir sonraki koşu için ortak bağlam sıfırlaması bekliyor",
};

export function CollaborationControls() {
  const root = useWorkspace((s) => s.root);
  const runId = useRun((s) => s.runId);
  const runStatus = useRun((s) => s.status);
  const runStage = useRun((s) => s.runStage);
  const engine = useRun((s) => s.engine);
  const running = runStatus === "running";
  const enabled = useCollaboration((s) => s.enabled);
  const setEnabled = useCollaboration((s) => s.setEnabled);
  const preview = useCollaboration((s) => s.preview);
  const approvalHandle = useCollaboration((s) => s.approvalHandle);
  const approvalProjectRoot = useCollaboration((s) => s.approvalProjectRoot);
  const attachedRunId = useCollaboration((s) => s.attachedRunId);
  const status = useCollaboration((s) => s.currentStatus);
  const busy = useCollaboration((s) => s.busy);
  const error = useCollaboration((s) => s.error);
  const recoveryConsent = useCollaboration((s) => s.recoveryConsent);
  const setRecoveryConsent = useCollaboration((s) => s.setRecoveryConsent);
  const getPreview = useCollaboration((s) => s.previewContext);
  const approve = useCollaboration((s) => s.approve);
  const discard = useCollaboration((s) => s.discard);
  const refreshStatus = useCollaboration((s) => s.refreshStatus);
  const invalidateDraft = useCollaboration((s) => s.invalidateDraft);
  const [open, setOpen] = useState(false);
  const [endpoint, setEndpoint] = useState("http://127.0.0.1:8765");
  const [memberId, setMemberId] = useState("");
  const [taskId, setTaskId] = useState("");
  const [credential, setCredential] = useState("");

  useEffect(() => {
    if (!runId || !enabled) return;
    let stopped = false;
    let inFlight = false;
    const poll = async () => {
      if (stopped || inFlight) return;
      inFlight = true;
      await refreshStatus(runId);
      inFlight = false;
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [enabled, runId, refreshStatus, root]);

  useEffect(() => { setCredential(""); setMemberId(""); setTaskId(""); }, [root]);

  const previewContext = () => {
    const secret = credential;
    setCredential("");
    void getPreview({ endpoint: endpoint.trim(), credential: secret, memberId: memberId.trim(), taskId: taskId.trim() });
  };
  const approvedHere = !!approvalHandle && approvalProjectRoot === root;
  const terminal = collaborationNeedsRecovery(status);
  const cursorReset = collaborationNeedsCursorReset(status);
  const attached = !!attachedRunId && attachedRunId === runId;
  const attachedWaitingUser = attached && runStatus === "done" && runStage === "ready" && engine === "pipeline" && !terminal;
  const configLocked = running || busy || attachedWaitingUser;
  const configChanged = (setter: (value: string) => void, value: string) => { setter(value); void invalidateDraft(); };

  return (
    <section className="mt-2 border-t border-border-w pt-2" aria-label="Ortak bağlam">
      <button type="button" className="flex w-full items-center justify-between py-1 text-left text-muted hover:text-text" aria-expanded={open} onClick={() => setOpen((value) => !value)} style={{ fontSize: "var(--t-label)" }}>
        <span>Ortak bağlam <span className="text-faint">(deneysel)</span></span><span aria-hidden="true">{open ? "−" : "+"}</span>
      </button>
      {open && <div className="space-y-2 pt-2" style={{ fontSize: "var(--t-caption)" }}>
        <label className="flex items-center gap-2 text-text">
          <input type="checkbox" checked={enabled} disabled={configLocked || !root} onChange={(event) => setEnabled(event.target.checked)} />
          Bu görevde ortak bağlamı kullan
        </label>
        <p className="text-muted">Paylaşılan bağlam native kodlayıcı girişine eklenir; planlayıcı/inceleyici eşzamanlanmaz. Bu bir LAN bağlantısı değildir.</p>
        {enabled && <>
          <label className="block text-muted">Sunucu uç noktası<input className="mt-1 w-full rounded-[var(--r-sm)] border border-border-w2 bg-field px-2 py-1.5 text-text" value={endpoint} disabled={configLocked} onChange={(event) => configChanged(setEndpoint, event.target.value)} autoComplete="url" /></label>
          <label className="block text-muted">Üye kimliği<input className="mt-1 w-full rounded-[var(--r-sm)] border border-border-w2 bg-field px-2 py-1.5 text-text" value={memberId} disabled={configLocked} onChange={(event) => configChanged(setMemberId, event.target.value)} /></label>
          <label className="block text-muted">Görev kimliği<input className="mt-1 w-full rounded-[var(--r-sm)] border border-border-w2 bg-field px-2 py-1.5 text-text" value={taskId} disabled={configLocked} onChange={(event) => configChanged(setTaskId, event.target.value)} /></label>
          <label className="block text-muted">Erişim bilgisi<input className="mt-1 w-full rounded-[var(--r-sm)] border border-border-w2 bg-field px-2 py-1.5 text-text" type="password" value={credential} disabled={configLocked} autoComplete="off" onChange={(event) => configChanged(setCredential, event.target.value)} /></label>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" disabled={running || busy || !root || !memberId.trim() || !taskId.trim() || !credential} onClick={previewContext}>{busy ? "Bekleyin…" : "Önizleme al"}</Button>
            {(preview || approvedHere) && <Button variant="secondary" disabled={running || busy} onClick={() => void discard()}>Önizlemeyi bırak</Button>}
          </div>
          {preview && <div className="space-y-1 rounded-[var(--r-sm)] border border-border-w bg-panel p-2 text-text" aria-live="polite">
            <div className="max-h-64 min-w-0 space-y-1 overflow-auto break-words [overflow-wrap:anywhere]"><p><strong>Görev:</strong> {preview.task.goal}</p>
            <p><strong>Üye / sahip:</strong> {preview.memberId} / {preview.task.owner}</p>
            <p><strong>Oturum:</strong> {preview.sessionId}</p>
            <p><strong>Proje kökü:</strong> {preview.projectRoot}</p>
            <p><strong>Taban commit:</strong> {preview.baseCommit}</p>
            <p><strong>Sürüm / revizyon:</strong> {preview.targetVersion} / {preview.revision}</p>
            <p><strong>Kapsam:</strong> {preview.task.scopes.join(", ") || "Belirtilmemiş"}</p>
            <p className="text-muted">Kapsam ve paylaşılan bağlam danışma verisidir; dosya kilidi, araç izni veya güncel yayın/CAS yetkisi değildir.</p>
            <p><strong>Paylaşılan hedef:</strong> {preview.context.goal}</p>
            {preview.context.decisions.length > 0 && <p><strong>Kararlar:</strong> {preview.context.decisions.join(" · ")}</p>}
            {Object.entries(preview.context.interfaces).map(([name, value]) => <p key={name}><strong>{name}:</strong> {value}</p>)}</div>
            {approvedHere ? <>
              <p className="text-ok">{terminal ? "Yerel onay mevcut; terminal kurtarmanın tamamlandığını göstermez." : "Bu proje için yerel onay verildi."}</p>
              {terminal && <Button variant="secondary" disabled={busy || running} onClick={() => void discard()}>Yeni kurtarma önizlemesi</Button>}
            </> : <Button variant="primary" disabled={busy || running || (cursorReset && !recoveryConsent)} onClick={() => void approve(cursorReset && recoveryConsent)}>{terminal ? "Bağlamı yeniden onayla" : "Gördüm, bu bağlamı onayla"}</Button>}
          </div>}
          {cursorReset && preview && !approvedHere && <label className="flex items-start gap-2 text-warn"><input type="checkbox" checked={recoveryConsent} disabled={running || busy} onChange={(event) => setRecoveryConsent(event.target.checked)} />Açıkça onayla: önizlenen revizyon için replay cursor’ını yeniden kur. Alınan revizyon uygulamanın kabul ettiği anlamına gelmez.</label>}
          {attachedWaitingUser && <>
            <p className="text-muted">Bu koşu ortak bağlamla bağlı. Mevcut koşunun kodlayıcısı ve bağlamı korunur.</p>
            {!busy && status && status.runId === runId && status.active === false && root && <ParticipantTaskStatus key={`${root}|${runId}|${status.sessionId}|${status.memberId}|${status.taskId}`} root={root} runId={runId!} sessionId={status.sessionId} memberId={status.memberId} taskId={status.taskId} />}
          </>}
          {status && <div className="space-y-1 text-muted" aria-live="polite">
            <p>Ortak oturum: {statusLabels[status.code ?? ""] ?? statusLabels[status.state] ?? status.state}{status.code ? ` · ${status.code}` : ""} {status.state === "unavailable" || status.active === null ? "(etkinlik doğrulanamadı)" : status.active ? "(etkin)" : "(etkin değil)"}</p>
            {status.recoveryState && <p>Kurtarma nedeni: {statusLabels[status.recoveryState] ?? status.recoveryState}. Kaynakların kapanması bu kurtarma gereğini otomatik kaldırmaz.</p>}
            <p>Alınan revizyon: {status.receivedRevision ?? "—"}; kodlayıcının tükettiği: {status.consumedRevision ?? "—"}; bekleyen: {status.state === "unavailable" ? "doğrulanamadı" : status.pendingCount}. Alınan revizyon, uygulamanın kabul ettiğini göstermez.</p>
          </div>}
          {error && <p role="alert" className="text-danger">{error}</p>}
        </>}
      </div>}
    </section>
  );
}
