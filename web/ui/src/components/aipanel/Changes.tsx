/* Changes — önerilen dosya değişiklikleri: checkbox'lı liste, satıra tık →
   merkez diff (Cursor deseni), Uygula / Vazgeç. changes_panel.py'nin halefi. */

import { FileDiff, FilePlus2, Check, X, RotateCcw, ShieldCheck } from "lucide-react";
import { useRun } from "@/state/run";
import { useEditor } from "@/state/editor";
import { fileIcon } from "@/lib/fileIcons";
import { Button, EmptyState } from "@/components/ui";
import { bridge } from "@/bridge";

export function Changes() {
  const diffs = useRun((s) => s.diffs);
  const verdict = useRun((s) => s.verdict);
  const verdictNote = useRun((s) => s.verdictNote);
  const engine = useRun((s) => s.engine);
  const evidence = useRun((s) => s.agentEvidence);
  const status = useRun((s) => s.status);
  const runStage = useRun((s) => s.runStage);
  const checkpointId = useRun((s) => s.checkpointId);
  const checkpointBusy = useRun((s) => s.checkpointBusy);
  const toggle = useRun((s) => s.toggleDiff);
  const apply = useRun((s) => s.apply);
  const reject = useRun((s) => s.reject);
  const restoreCheckpoint = useRun((s) => s.restoreCheckpoint);
  const openDiff = useEditor((s) => s.openDiff);
  const activeDiff = useEditor((s) => s.diff?.path ?? null);

  if (diffs.length === 0) {
    const ok = runStage === "applied" || runStage === "restored";
    const [EmptyIcon, title, description] =
      runStage === "applied"
        ? ([ShieldCheck, "Değişiklikler uygulandı",
            "Bu turun checkpoint'i geri alma için hazır."] as const)
        : runStage === "restored"
          ? ([RotateCcw, "Checkpoint geri yüklendi",
              "Dosyalar checkpoint anına döndürüldü. Yeni bir tur başlatabilirsiniz."] as const)
          : runStage === "ready"
            ? ([FileDiff, "Uygulanacak dosya yok",
                engine === "agent" ? "Ajan bu tur için dosya değişikliği önermedi." : "Ekip bu tur için dosya değişikliği önermedi."] as const)
            : ([FileDiff, "Öneri bekleniyor",
                engine === "agent" ? "Ajan sonucu ve varsa doğrulama kanıtı burada görünür." : "Ekip incelemeyi bitirdiğinde dosya değişiklikleri burada listelenir."] as const);
    return (
      <div className="h-full overflow-y-auto">
      {engine === "agent" && evidence && <div className="border-b border-border-w px-3 py-3 text-text2">
        <p className="text-warn" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>{agentVerificationSummary(evidence)}</p>
        {evidence.agent_message && <p className="mt-1 break-words whitespace-pre-wrap" style={{ fontSize: "var(--t-caption)" }}>{evidence.agent_message}</p>}
        <p className="mt-1 break-words" style={{ fontSize: "var(--t-caption)" }}>Değişen yollar: {evidence.changed_paths.length ? evidence.changed_paths.join(", ") : "—"}</p>
        {evidence.verification.checks.map((check, i) => <p key={`${check.check_id}-${i}`} className="break-words" style={{ fontSize: "var(--t-caption)" }}>{check.check_id}: {check.status}{!bridge.isNative || check.check_id.startsWith("mock") ? " · MOCK, gerçek doğrulama değil" : ""}</p>)}
      </div>}
      <EmptyState
        icon={EmptyIcon}
        tone={ok ? "ok" : "neutral"}
        title={title}
        description={description}
        className="h-full"
        action={
          runStage === "applied" && checkpointId ? (
            <Button
              variant="warn-outline"
              size="sm"
              icon={RotateCcw}
              loading={checkpointBusy}
              aria-busy={checkpointBusy}
              onClick={() => void restoreCheckpoint()}
            >
              Geri al
            </Button>
          ) : undefined
        }
      />
      </div>
    );
  }

  const checkedCount = diffs.filter((d) => d.checked).length;
  const canApply = runStage === "ready" && status !== "running" && checkedCount > 0 && !checkpointBusy;

  return (
    <div className="flex h-full flex-col">
      <div className="shrink-0 border-b border-border-w px-3 py-3">
        <div className="flex items-start gap-2">
          <ShieldCheck size={14} className={engine !== "agent" && verdict === "APPROVED" ? "mt-0.5 shrink-0 text-ok" : "mt-0.5 shrink-0 text-warn"} />
          <div className="min-w-0 flex-1">
            <p className="text-warn" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>{engine === "agent" ? "Sonuç / doğrulama" : verdict === "APPROVED" ? "İnceleme onayı" : "İnceleme notu"}</p>
            <p className="text-text2" style={{ fontSize: "var(--t-caption)", lineHeight: 1.35 }}>
              {engine === "agent" ? (evidence ? agentVerificationSummary(evidence) : "Kanıt bekleniyor. Değişiklikleri insan olarak inceleyebilirsiniz.") : verdictNote || `${checkedCount}/${diffs.length} dosya seçili. Uygulamadan önce kontrol edin.`}
            </p>
            <p className="mt-0.5 text-faint" style={{ fontSize: "var(--t-caption)" }}>{checkedCount}/{diffs.length} dosya uygulamaya dahil</p>
            {engine === "agent" && evidence && <>
              {evidence.agent_message && <p className="mt-2 break-words whitespace-pre-wrap text-text2" style={{ fontSize: "var(--t-caption)" }}>{evidence.agent_message}</p>}
              <p className="mt-1 break-words text-text2" style={{ fontSize: "var(--t-caption)" }}>Değişen yollar: {evidence.changed_paths.length ? evidence.changed_paths.join(", ") : "—"}</p>
              {evidence.verification.checks.map((check, i) => <p key={`${check.check_id}-${i}`} className="break-words text-text2" style={{ fontSize: "var(--t-caption)" }}>{check.check_id}: {check.status}{!bridge.isNative || check.check_id.startsWith("mock") ? " · MOCK, gerçek doğrulama değil" : ""}</p>)}
              {checkedCount !== diffs.length && <p className="mt-1 text-warn" style={{ fontSize: "var(--t-caption)" }}>Kısmi dosya seçimi ayrı olarak doğrulanmadı.</p>}
            </>}
          </div>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {diffs.map((d) => {
          const name = d.path.split("/").pop() ?? d.path;
          const { Icon, color } = fileIcon(name);
          const active = activeDiff === d.path;
          return (
            <div
              key={d.path}
              role="button"
              tabIndex={0}
              onClick={() => void openDiff(d.path)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") { e.preventDefault(); void openDiff(d.path); }
              }}
              className={
                "flex cursor-pointer items-center gap-2 border-l-2 px-2 py-2 " +
                (active ? "border-accent bg-accentdim/60" : "border-transparent hover:bg-card/45")
              }
            >
              <input
                type="checkbox"
                aria-label={`${d.path} değişikliğini seç`}
                checked={d.checked}
                onChange={() => toggle(d.path)}
                onClick={(e) => e.stopPropagation()}
                className="size-3.5 shrink-0 accent-[var(--accent)]"
              />
              <Icon size={14} strokeWidth={1.8} style={{ color }} className="shrink-0" />
              <span className="min-w-0 flex-1 truncate text-text2" style={{ fontSize: "var(--t-label)" }}>
                {d.path}
              </span>
              {d.isNew && <span className="flex shrink-0 items-center gap-1 text-ok" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}><FilePlus2 size={10} /> Yeni</span>}
            </div>
          );
        })}
      </div>

      <div className="border-t border-border-w bg-panel px-2.5 py-3">
        <div className="mb-2 flex items-center gap-1.5 text-muted" style={{ fontSize: "var(--t-caption)" }}>
          <ShieldCheck size={12} className="text-ok" /> Uygulamadan önce checkpoint alınır.
        </div>
        <div className="flex gap-2">
          <Button
            variant="primary"
            size="sm"
            icon={Check}
            block
            onClick={() => void apply()}
            disabled={!canApply}
            loading={checkpointBusy}
            aria-busy={checkpointBusy}
          >
            {checkpointBusy ? "Uygulanıyor…" : `Uygula (${checkedCount})`}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            icon={X}
            onClick={() => void reject()}
            disabled={checkpointBusy}
          >
            Vazgeç
          </Button>
        </div>
      </div>
    </div>
  );
}

function agentVerificationSummary(evidence: NonNullable<ReturnType<typeof useRun.getState>["agentEvidence"]>) {
  const v = evidence.verification;
  const mock = !bridge.isNative || evidence.execution_id.startsWith("mock-") || v.checks.some((check) => check.check_id.startsWith("mock"));
  if (mock) return `MOCK senaryosu (${v.outcome}); gerçek doğrulama kanıtı değil.`;
  const sha = evidence.diff_sha256;
  const validHash = typeof sha === "string" && /^[a-f0-9]{64}$/i.test(sha);
  const validIds = typeof v.verification_id === "string" && v.verification_id.length > 0 && typeof v.plan_id === "string" && v.plan_id.length > 0;
  const allChecksPass = v.checks.length > 0 && v.checks.every((check) => check.status === "pass" && !check.check_id.startsWith("mock"));
  if (!evidence.unknown && !evidence.truncated && evidence.reason === "single_agent_proposal" && v.outcome === "pass" && v.fingerprint_complete === true && v.changed_content === false && validHash && validIds && allChecksPass) return "Doğrulama geçti; fingerprint tam ve içerik değişmedi.";
  return `Henüz doğrulanmadı (${v.outcome || "sonuç bilinmiyor"}). İnsan incelemesi ve açık uygulama kararı mümkün.`;
}
