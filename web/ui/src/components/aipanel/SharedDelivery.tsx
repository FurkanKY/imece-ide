import { useEffect, useMemo } from "react";
import { bridge } from "@/bridge";
import { Button } from "@/components/ui";
import { useCollaboration } from "@/state/collaboration";
import { useDelivery } from "@/state/delivery";
import { useRun } from "@/state/run";
import { useWorkspace } from "@/state/workspace";

const inputClass = "min-w-0 rounded-[var(--r-sm)] border border-border-w bg-field px-2 py-1.5 text-text outline-none focus-visible:border-accent";

export function SharedDelivery() {
  const root = useWorkspace((s) => s.root);
  const runId = useRun((s) => s.runId);
  const diffs = useRun((s) => s.diffs);
  const runStage = useRun((s) => s.runStage);
  const status = useRun((s) => s.status);
  const engine = useRun((s) => s.engine);
  const attachedRunId = useCollaboration((s) => s.attachedRunId);
  const storePath = useDelivery((s) => s.storePath);
  const hubPath = useDelivery((s) => s.hubPath);
  const outputPath = useDelivery((s) => s.outputPath);
  const preview = useDelivery((s) => s.preview);
  const previewSelectionPaths = useDelivery((s) => s.previewSelectionPaths);
  const proposals = useDelivery((s) => s.proposals);
  const selectedIds = useDelivery((s) => s.selectedProposalIds);
  const candidate = useDelivery((s) => s.candidate);
  const conflicts = useDelivery((s) => s.conflicts);
  const error = useDelivery((s) => s.error);
  const busy = useDelivery((s) => s.busy);
  const verify = useDelivery((s) => s.verify);
  const allowOutOfScope = useDelivery((s) => s.allowOutOfScope);
  const publishedReceipt = useDelivery((s) => s.publishedReceipt);
  const configure = useDelivery((s) => s.configure);
  const setVerify = useDelivery((s) => s.setVerify);
  const setAllowOutOfScope = useDelivery((s) => s.setAllowOutOfScope);
  const toggleProposal = useDelivery((s) => s.toggleProposal);
  const reset = useDelivery((s) => s.reset);
  const previewDelivery = useDelivery((s) => s.previewDelivery);
  const publish = useDelivery((s) => s.publish);
  const refresh = useDelivery((s) => s.refresh);
  const combine = useDelivery((s) => s.combine);
  const discardPreview = useDelivery((s) => s.discardPreview);
  const selectedPaths = useMemo(() => [...new Set(diffs.filter((d) => d.checked).map((d) => d.path))].sort(), [diffs]);
  const pathsKey = selectedPaths.join("\n");
  const ready = !!root && !!runId && attachedRunId === runId && runStage === "ready" && status !== "running" && engine === "pipeline";
  const previewMatches = !!preview && preview.runId === runId && previewSelectionPaths.length === selectedPaths.length && previewSelectionPaths.every((path, i) => path === selectedPaths[i]) && preview.paths.every((path) => previewSelectionPaths.includes(path));
  const omittedPaths = previewSelectionPaths.filter((path) => !preview?.paths.includes(path));

  useEffect(() => { reset(root, runId); }, [root, runId, reset]);
  useEffect(() => {
    if (preview && !previewMatches) void discardPreview();
  }, [pathsKey, preview, previewMatches, discardPreview]);

  return (
    <section className="flex h-full flex-col overflow-y-auto px-3 py-3" aria-label="Ortak aday teslimi">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div>
          <h2 className="text-text" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>Ortak aday</h2>
          <p className="mt-0.5 text-muted" style={{ fontSize: "var(--t-caption)" }}>Bekleyen ekip çalışmasını açık onayla teslim edin.</p>
        </div>
        {!bridge.isNative && <span className="rounded-[var(--r-sm)] border border-warn/40 px-1.5 py-0.5 text-warn" style={{ fontSize: "var(--t-caption)" }}>MOCK</span>}
      </div>

      {!ready ? (
        <p className="border-y border-border-w py-3 text-text2" style={{ fontSize: "var(--t-caption)", lineHeight: 1.5 }}>
          Önce bu projede native işbirliğiyle bir <b>pipeline</b> koşusu başlatın. Teslim yalnızca o koşunun bekleyen kullanıcı onaylı önerisinde kullanılabilir; normal koşular ve başka projeler desteklenmez. Yerel bare store ve hub dizin yolları bu koşu hazır olduğunda burada girilir; localhost URL veya servis başlatımı kullanılmaz.
        </p>
      ) : (
        <>
          <div className="grid gap-2 border-y border-border-w py-3">
            <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>
              Yerel bare Git store dizini <input disabled={busy} className={inputClass} value={storePath} onChange={(e) => configure("storePath", e.target.value)} placeholder="/local/path/code-channel.git" />
            </label>
            <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>
              Yerel Code Channel hub dizini <input disabled={busy} className={inputClass} value={hubPath} onChange={(e) => configure("hubPath", e.target.value)} placeholder="/local/path/hub.git" />
            </label>
            <p className="text-faint" style={{ fontSize: "var(--t-caption)", lineHeight: 1.45 }}>Bunlar yerel dosya sistemi yollarıdır; HTTP uç noktası veya kimlik bilgisi girilmez. Bellekte tutulur, tercihlere kaydedilmez.</p>
          </div>

          <div className="py-3">
            <div className="mb-1 flex items-center justify-between">
              <span className="text-text2" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>Bu koşuda onaylı dosyalar</span>
              <span className="text-muted" style={{ fontSize: "var(--t-caption)" }}>{selectedPaths.length}/64</span>
            </div>
            <p className="mb-2 text-faint" style={{ fontSize: "var(--t-caption)", lineHeight: 1.45 }}>En fazla 64 yolu açıkça seçin. Seçili dosyaların mevcut çalışma ağacı baytları, oturumun yakalanmış tabanına göre teslim edilir; seçili ama tabana göre değişmemiş yollar bilette yer almayabilir. Dahil edilen baytlar önceki yerel WIP değişikliklerini içerebilir. Geçmiş seçilmez, gömülü sırlar otomatik ayıklanmaz.</p>
            <p className="mb-2 text-muted" style={{ fontSize: "var(--t-caption)", lineHeight: 1.45 }}>Görev kapsamları danışma etiketidir, dosya kilidi veya paylaşım izni değildir. Yayın yalnız bu açık yol seçimi ve ayrıca verilen bilet onayıyla yapılır.</p>
            {selectedPaths.length ? <ul className="max-h-24 overflow-auto text-text2" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{selectedPaths.map((path) => <li key={path} className="truncate py-0.5">{path}</li>)}</ul> : <p className="text-muted" style={{ fontSize: "var(--t-caption)" }}>İnceleme sekmesinde teslim edilecek dosyaları seçin.</p>}
            <Button className="mt-2" size="sm" loading={busy} disabled={!selectedPaths.length || selectedPaths.length > 64 || !storePath.trim() || !hubPath.trim()} onClick={() => root && runId && void previewDelivery({ root, runId, paths: selectedPaths })}>Önizleme al</Button>
          </div>

          {preview && previewMatches && <div className="border-y border-border-w py-3">
            <h3 className="text-text2" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>Değişmez teslim bileti · {preview.fileCount} dosya · {preview.artifactBytes} bayt</h3>
            <p className="mt-1 break-all text-muted" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>Öneri {preview.proposalId} · rev {preview.expectedRevision}<br />SHA-256 {preview.contentDigest}</p>
            <ul className="mt-1 text-text2" style={{ fontSize: "var(--t-caption)" }}>{preview.paths.map((path) => <li key={path}>{path}</li>)}</ul>
            {omittedPaths.length > 0 && <p className="mt-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Açıkça seçildi ancak bu bilette değişmemiş olduğu için yer almıyor: {omittedPaths.join(", ")}</p>}
            {preview.warnings.map((warning, i) => <p key={i} className="mt-1 text-warn" style={{ fontSize: "var(--t-caption)" }}>{warning}</p>)}
            {!!preview.outOfScopePaths.length && <div className="mt-2">
              <p className="text-warn" style={{ fontSize: "var(--t-caption)" }}>Kapsam dışında: {preview.outOfScopePaths.join(", ")}</p>
              <label className="mt-1 flex items-start gap-2 text-text2" style={{ fontSize: "var(--t-caption)" }}><input type="checkbox" disabled={busy} checked={allowOutOfScope} onChange={(e) => setAllowOutOfScope(e.target.checked)} />Bu kapsam dışı yolların yayımlanmasına ayrıca izin veriyorum.</label>
            </div>}
            <div className="mt-2 flex flex-wrap gap-2">
              <Button size="sm" variant="primary" loading={busy} disabled={!previewMatches || (!!preview.outOfScopePaths.length && !allowOutOfScope)} onClick={() => root && runId && void publish(root, runId)}>Bu bileti yayımla</Button>
              <Button size="sm" disabled={busy} onClick={() => void discardPreview()}>Bileti bırak</Button>
            </div>
          </div>}

          {publishedReceipt && <p className="border-b border-border-w py-2 text-ok" style={{ fontSize: "var(--t-caption)" }}>Yayımlandı: {publishedReceipt}. Hub kaydı korunur; yeni köke aktarılmaz.</p>}

          <div className="py-3">
            <div className="flex items-center justify-between gap-2">
              <h3 className="text-text2" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>Gelen ekip önerileri</h3>
              <Button size="sm" loading={busy} disabled={!storePath.trim() || !hubPath.trim()} onClick={() => root && runId && void refresh(root, runId)}>Listeyi yenile</Button>
            </div>
            <p className="my-1 text-faint" style={{ fontSize: "var(--t-caption)" }}>Liste yalnızca öneri metaverisini gösterir; içerik bu ekrana alınmaz. Her görev için güncel bağlamı eşleşen tek bir öneriyi elle seçin (en fazla 16 öneri). Aynı görevdeki başka öneriyi seçmek için önce mevcut seçimi kaldırın.</p>
            {proposals.length === 0 ? <p className="text-muted" style={{ fontSize: "var(--t-caption)" }}>Henüz listelenmiş öneri yok. Yenilemeyi siz başlatın.</p> : <ul className="divide-y divide-border-w">
              {proposals.map((proposal) => <li key={proposal.proposalId} className="py-2">
                <label className="flex items-start gap-2 text-text2" style={{ fontSize: "var(--t-caption)" }}>
                  <input type="checkbox" checked={selectedIds.includes(proposal.proposalId)} disabled={!proposal.currentContextHashMatches || (selectedIds.length >= 16 && !selectedIds.includes(proposal.proposalId)) || busy} onChange={() => toggleProposal(proposal.proposalId)} />
                  <span className="min-w-0"><span className="block break-all text-text">{proposal.proposalId}</span>{proposal.owner} · {proposal.taskId} · {proposal.fileCount} dosya · {proposal.proposalRevision}
                    {!proposal.currentContextHashMatches && <span className="block text-warn">Eski bağlam kanıtı — birleştirme devre dışı; listeyi yenileyin.</span>}
                    {proposal.currentContextHashMatches && <span className="block text-muted">Bağlam eşleşiyor · {proposal.contextRevision}</span>}
                  </span>
                </label>
              </li>)}
            </ul>}
            <label className="mt-2 grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>
              Yeni aday dizini (checkout/worktree/store/hub dışında olmalı)
              <input disabled={busy} className={inputClass} value={outputPath} onChange={(e) => configure("outputPath", e.target.value)} placeholder="/tmp/imece-candidate-unique" />
            </label>
            <label className="mt-2 flex items-start gap-2 text-text2" style={{ fontSize: "var(--t-caption)" }}><input type="checkbox" disabled={busy} checked={verify} onChange={(e) => setVerify(e.target.checked)} />Tam doğrulama bu yeni dizinde proje komut/kodunu çalıştırır; sandbox değildir.</label>
            <Button className="mt-2" size="sm" loading={busy} disabled={!selectedIds.length || !outputPath.trim() || selectedIds.some((id) => !proposals.find((p) => p.proposalId === id)?.currentContextHashMatches)} onClick={() => root && runId && void combine(root, runId)}>Adayı birleştir</Button>
          </div>

          {conflicts.length > 0 && <div className="border-y border-warn/40 py-2"><p className="text-warn" style={{ fontSize: "var(--t-caption)" }}>Çakışmalar — aday oluşturulmadı</p><ul className="mt-1 text-text2" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{conflicts.map((path) => <li key={path} className="break-all">{path}</li>)}</ul></div>}
          {candidate && <div className="border-y border-border-w py-3">
            <h3 className="text-text" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>Aday · {candidate.file_count} dosya</h3>
            <p className="break-all text-muted" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{candidate.candidate_dir}<br />Parmak izi: {candidate.content_fingerprint}</p>
            <p className="mt-1 text-text2" style={{ fontSize: "var(--t-caption)" }}>Doğrulama: {candidate.verification.status === "pass" && candidate.verification.fingerprint_complete === true && candidate.verification.changed_content === false ? "Doğrulandı" : candidate.verification.status === "not_run" ? "Çalıştırılmadı" : candidate.verification.status === "fail" ? "Doğrulama başarısız" : candidate.verification.status === "pass" ? "Tam doğrulama kanıtı eksik" : `Başarılı değil (${candidate.verification.status})`}</p>
            {candidate.verification.checks?.map((check, i) => <pre key={i} className="mt-1 overflow-auto whitespace-pre-wrap text-faint" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{JSON.stringify(check)}</pre>)}
            {candidate.notes.map((note, i) => <p key={i} className="mt-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>{note}</p>)}
          </div>}
        </>
      )}
      {error && <p role="alert" className="mt-2 text-err" style={{ fontSize: "var(--t-caption)" }}>{error}</p>}
    </section>
  );
}
