import { useEffect, useMemo, useRef, useState } from "react";
import { bridge, OwnerShare } from "@/bridge";
import { Button } from "@/components/ui";
import { useCollaboration } from "@/state/collaboration";
import { useDelivery } from "@/state/delivery";
import { useOwner } from "@/state/owner";
import { useRun } from "@/state/run";
import { useWorkspace } from "@/state/workspace";

const input = "min-w-0 rounded-[var(--r-sm)] border border-border-w bg-field px-2 py-1.5 text-text outline-none focus-visible:border-accent";
const lines = (text: string) => text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
const initialTasks = [{ id: "task-1", owner: "", goal: "", scopes: "" }];

export function OwnerSession() {
  const root = useWorkspace((s) => s.root);
  const runId = useRun((s) => s.runId);
  const status = useOwner((s) => s.status);
  const preview = useOwner((s) => s.preview);
  const draftRoot = useOwner((s) => s.draftRoot);
  const busy = useOwner((s) => s.busy);
  const error = useOwner((s) => s.error);
  const refresh = useOwner((s) => s.refresh);
  const previewCreate = useOwner((s) => s.previewCreate);
  const create = useOwner((s) => s.create);
  const select = useOwner((s) => s.select);
  const start = useOwner((s) => s.start);
  const stop = useOwner((s) => s.stop);
  const forgetPreview = useOwner((s) => s.forgetPreview);
  const adopt = useCollaboration((s) => s.adoptLocalPreview);
  const adoptPaths = useDelivery((s) => s.adoptChannelPaths);
  const [mode, setMode] = useState<"create" | "existing">("create");
  const [editing, setEditing] = useState(false);
  const [sessionId, setSessionId] = useState("imece-session");
  const [version, setVersion] = useState("v1");
  const [goal, setGoal] = useState("");
  const [ownerId, setOwnerId] = useState("owner");
  const [memberText, setMemberText] = useState("owner\nmember-1");
  const [tasks, setTasks] = useState(initialTasks);
  const [storePath, setStorePath] = useState("");
  const [hubPath, setHubPath] = useState("");
  const [shareMember, setShareMember] = useState("");
  const [confirmSecret, setConfirmSecret] = useState(false);
  const [share, setShare] = useState<OwnerShare | null>(null);
  const [sharing, setSharing] = useState(false);
  const [localPreviewBusy, setLocalPreviewBusy] = useState(false);
  const [localMember, setLocalMember] = useState("");
  const [localTask, setLocalTask] = useState("");
  const [handoff, setHandoff] = useState<Awaited<ReturnType<typeof bridge.call<"collab.owner.localPreview">>> | null>(null);
  const [handoffSeen, setHandoffSeen] = useState(false);
  const [copyNotice, setCopyNotice] = useState("");
  const mounted = useRef(false);
  const localFlight = useRef(false);
  const shareFlight = useRef(false);
  const handoffGeneration = useRef(0);
  const handoffId = useRef<string | null>(null);
  const shareGeneration = useRef(0);
  const members = useMemo(() => [...new Set(lines(memberText))], [memberText]);
  const current = !!root && status?.projectRoot === root;
  const serverLocked = !!status && ["running", "starting", "stopping", "cleanup_failed"].includes(status.state);
  const otherRunning = serverLocked && !current;
  const running = current && status?.state === "running";
  const runStatus = useRun((s) => s.status);
  const runStage = useRun((s) => s.runStage);

  const clearHandoff = () => {
    handoffGeneration.current += 1;
    const previewId = handoffId.current;
    handoffId.current = null;
    setHandoff(null); setHandoffSeen(false);
    if (previewId) void bridge.call("collab.discard", { previewId }).catch(() => undefined);
  };
  const clearShare = () => {
    shareGeneration.current += 1;
    setShare(null); setConfirmSecret(false); setCopyNotice(""); setSharing(false);
  };
  const stopOwned = async () => {
    clearShare(); clearHandoff();
    return stop();
  };

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, 1000);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
      shareGeneration.current += 1;
      handoffGeneration.current += 1;
      const previewId = handoffId.current;
      handoffId.current = null;
      if (previewId) void bridge.call("collab.discard", { previewId }).catch(() => undefined);
    };
  }, [refresh]);
  useEffect(() => {
    useOwner.getState().resetRoot(root);
    clearShare(); clearHandoff(); setEditing(false);
    setMode("create"); setSessionId("imece-session"); setVersion("v1"); setGoal("");
    setOwnerId("owner"); setMemberText("owner\nmember-1"); setTasks(initialTasks);
    setStorePath(""); setHubPath("");
    setLocalMember(""); setLocalTask("");
  }, [root]);
  useEffect(() => {
    if (share && (status?.epoch !== share.epoch || !running || status.projectRoot !== root)) clearShare();
    if (handoff && (!running || status?.epoch !== handoff.epoch || status.projectRoot !== root || runStatus === "running" || runStage === "ready")) clearHandoff();
  }, [status?.epoch, status?.projectRoot, running, root, share, handoff, runStatus, runStage]);

  const canPreview = !!root && draftRoot === root && !!sessionId.trim() && sessionId.length <= 128 && !!version.trim() && version.length <= 128 && !!goal.trim() && goal.length <= 4096 && !!ownerId.trim() && ownerId.length <= 128 && members.includes(ownerId) && members.length > 0 && members.length <= 256 && members.every((m) => m.length <= 128) && tasks.length > 0 && tasks.length <= 256 && new Set(tasks.map((t) => t.id)).size === tasks.length && tasks.every((t) => !!t.id.trim() && t.id.length <= 128 && !!t.goal.trim() && t.goal.length <= 4096 && members.includes(t.owner) && lines(t.scopes).length > 0 && lines(t.scopes).every((scope) => scope.length <= 512));
  const changeTask = (index: number, field: keyof typeof tasks[number], value: string) => setTasks((all) => all.map((task, i) => i === index ? { ...task, [field]: value } : task));

  const showShare = async () => {
    if (shareFlight.current || sharing || !shareMember || !confirmSecret || !running || !membersForStatus.includes(shareMember)) return;
    const token = ++shareGeneration.current;
    shareFlight.current = true;
    setCopyNotice(""); setSharing(true);
    try {
      const result = await bridge.call("collab.owner.shareOnce", { memberId: shareMember, confirmSecret: true });
      const latest = useOwner.getState().status;
      if (mounted.current && token === shareGeneration.current && useWorkspace.getState().root === root && latest?.projectRoot === root && latest.state === "running" && latest.epoch === result.epoch && result.memberId === shareMember) setShare(result);
    }
    catch (e) { if (mounted.current && token === shareGeneration.current) useOwner.setState({ error: e instanceof Error ? e.message : "Erişim bilgisi verilemedi." }); }
    finally { shareFlight.current = false; if (mounted.current && token === shareGeneration.current) setSharing(false); }
  };
  const membersForStatus = status?.memberIds ?? [];
  const copyShare = async () => {
    if (!share) return;
    const selected = share;
    const token = shareGeneration.current;
    const { credential, ...metadata } = selected;
    setShare(null); setConfirmSecret(false);
    try {
      await bridge.call("app.clipboardWrite", { text: JSON.stringify({ ...metadata, credential }, null, 2) });
      const latest = useOwner.getState().status;
      if (mounted.current && token === shareGeneration.current && useWorkspace.getState().root === root && latest?.projectRoot === root && latest.state === "running" && latest.epoch === selected.epoch) setCopyNotice("Kopyalandı. Pano içeriği uygulama dışındadır; güvenli biçimde temizlemek size bağlıdır.");
    } catch { if (mounted.current && token === shareGeneration.current) setCopyNotice("Panoya kopyalanamadı; erişim bilgisini paylaşmayın."); }
  };
  const loadLocalPreview = async () => {
    if (localFlight.current || !root || !localMember || !localTask || runStatus === "running" || runStage === "ready") return;
    clearHandoff();
    const token = ++handoffGeneration.current;
    const selectedRoot = root, selectedMember = localMember, selectedTask = localTask;
    const expectedEpoch = status?.epoch;
    localFlight.current = true;
    setLocalPreviewBusy(true);
    try {
      const result = await bridge.call("collab.owner.localPreview", { memberId: selectedMember, taskId: selectedTask });
      const latest = useOwner.getState().status;
      if (!mounted.current || token !== handoffGeneration.current || useWorkspace.getState().root !== selectedRoot || result.preview.projectRoot !== selectedRoot || latest?.state !== "running" || latest.projectRoot !== selectedRoot || latest.epoch !== result.epoch || result.epoch !== expectedEpoch || localMember !== selectedMember || localTask !== selectedTask) {
        await bridge.call("collab.discard", { previewId: result.preview.previewId }).catch(() => undefined);
        return;
      }
      handoffId.current = result.preview.previewId;
      setHandoff(result); setHandoffSeen(false);
    }
    catch (e) { if (mounted.current && token === handoffGeneration.current) useOwner.setState({ error: e instanceof Error ? e.message : "Yerel görev önizlenemedi." }); }
    finally { localFlight.current = false; if (mounted.current) setLocalPreviewBusy(false); }
  };
  const adoptHandoff = () => {
    const latest = useOwner.getState().status;
    if (!root || !handoff || !handoffSeen || runStatus === "running" || runStage === "ready" || useWorkspace.getState().root !== root || latest?.state !== "running" || latest.projectRoot !== root || latest.epoch !== handoff.epoch || handoff.preview.projectRoot !== root) return;
    if (adopt(handoff.preview, root)) {
      adoptPaths(root, runId, handoff.storePath, handoff.hubPath);
      handoffId.current = null;
      setHandoff(null); setHandoffSeen(false);
    }
  };
  const canAdoptHandoff = !!root && !!handoff && handoffSeen && runStatus !== "running" && runStage !== "ready" && status?.state === "running" && status.projectRoot === root && status.epoch === handoff.epoch && handoff.preview.projectRoot === root;

  const editConfiguration = async () => {
    if (!current || !status || serverLocked || busy) return;
    if (status.state === "creation_failed") { setEditing(true); return; }
    const stopped = await stop();
    const latest = useOwner.getState().status;
    if (stopped && latest?.projectRoot === root && latest.state === "stopped") setEditing(true);
  };

  return <section className="flex h-full flex-col gap-3 overflow-y-auto px-3 py-3" aria-label="Sahip oturumu">
    <div><h2 className="text-text" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>Yerel sahip oturumu</h2><p className="mt-1 text-muted" style={{ fontSize: "var(--t-caption)", lineHeight: 1.45 }}>Yalnız yerel metadata ve açıkça başlatılan 127.0.0.1 sunucusu. Bu akış görev çalıştırmaz veya yayımlamaz.</p></div>
    {!bridge.isNative && <p className="text-warn" style={{ fontSize: "var(--t-caption)" }}>MOCK: dosya sistemi, listener ve gerçek kimlik bilgisi yok; erişim bilgisi simülasyondur.</p>}
    {otherRunning && <div role="status" className="border-y border-warn/40 py-2 text-warn" style={{ fontSize: "var(--t-caption)" }}>Sunucu {status?.projectRoot} için açık; başka oturum seçmeden durdurun.<Button className="ml-2" size="sm" loading={busy} onClick={() => void stopOwned()}>Durdur</Button></div>}
    {root ? <>
      {current && !serverLocked && !editing && status?.state !== "creation_failed" && <div className="border-y border-border-w py-3"><p className="mb-2 text-muted" style={{ fontSize: "var(--t-caption)" }}>Bu yapılandırmayı değiştirmek için önce mevcut listener'ı tamamen durdurun. Metadata silinmez veya otomatik taşınmaz.</p><Button size="sm" loading={busy} onClick={() => void editConfiguration()}>Yapılandırmayı değiştir</Button></div>}
      {current && status?.state === "creation_failed" && <div className="border-y border-warn/40 py-2 text-warn" style={{ fontSize: "var(--t-caption)" }}>Metadata oluşturma tamamlanamadı. Kapatma yeniden denemesi bu durumu onarmaz. Oluşturulmuş yolları koruyun; mevcut metadata seçebilir veya yeni bir önizlemeyle kurtarmayı deneyebilirsiniz. Otomatik silme yapılmaz.</div>}
      {(!current || editing || status?.state === "creation_failed") && !otherRunning && <div className="flex gap-2 border-b border-border-w pb-2"><Button size="sm" variant={mode === "create" ? "primary" : "secondary"} onClick={() => { setMode("create"); forgetPreview(); }}>Yeni metadata</Button><Button size="sm" variant={mode === "existing" ? "primary" : "secondary"} onClick={() => { setMode("existing"); forgetPreview(); }}>Mevcut metadata</Button></div>}
      {(!current || editing || status?.state === "creation_failed") && !otherRunning && mode === "create" && <>
        <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Oturum kimliği<input className={input} maxLength={128} value={sessionId} onChange={(e) => { setSessionId(e.target.value); forgetPreview(); }} /></label>
        <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Hedef sürüm<input className={input} maxLength={128} value={version} onChange={(e) => { setVersion(e.target.value); forgetPreview(); }} /></label>
        <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Oturum hedefi<textarea className={input} maxLength={4096} value={goal} onChange={(e) => { setGoal(e.target.value); forgetPreview(); }} /></label>
        <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Sahip kimliği<input className={input} maxLength={128} value={ownerId} onChange={(e) => { setOwnerId(e.target.value); forgetPreview(); }} /></label>
        <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Üye kimlikleri · satır veya virgülle ayırın<textarea className={input} maxLength={32768} value={memberText} onChange={(e) => { setMemberText(e.target.value); forgetPreview(); }} /><span className="text-faint">Sahip kimliği <b>{ownerId}</b> de bu listeye açıkça eklenmelidir. Görev atamaları listeye üye eklemez.</span></label>
        {tasks.map((task, i) => <fieldset key={i} className="grid gap-1 border-y border-border-w py-2"><legend className="text-text2" style={{ fontSize: "var(--t-caption)" }}>Görev {i + 1}</legend>
          <input className={input} maxLength={128} aria-label="Görev kimliği" placeholder="Görev kimliği" value={task.id} onChange={(e) => { changeTask(i, "id", e.target.value); forgetPreview(); }} />
          <select className={input} aria-label="Atanmış üye" value={task.owner} onChange={(e) => { changeTask(i, "owner", e.target.value); forgetPreview(); }}><option value="">Üye seçin</option>{members.map((member) => <option key={member} value={member}>{member}</option>)}</select>
          <textarea className={input} maxLength={4096} aria-label="Görev amacı" placeholder="Görev amacı" value={task.goal} onChange={(e) => { changeTask(i, "goal", e.target.value); forgetPreview(); }} />
          <textarea className={input} aria-label="Kapsam yolları" placeholder="Kapsam yolları · satır başına tam yol öneki" value={task.scopes} onChange={(e) => { changeTask(i, "scopes", e.target.value); forgetPreview(); }} />
          {tasks.length > 1 && <Button size="sm" onClick={() => { setTasks((all) => all.filter((_, j) => j !== i)); forgetPreview(); }}>Görevi kaldır</Button>}
        </fieldset>)}
        {tasks.length < 4 && <Button size="sm" disabled={busy} onClick={() => { setTasks((all) => [...all, { id: `task-${all.length + 1}`, owner: "", goal: "", scopes: "" }]); forgetPreview(); }}>Görev ekle</Button>}
        <Button size="sm" loading={busy} disabled={!canPreview || busy} onClick={() => root && void previewCreate(root, { sessionId, targetVersion: version, goal, ownerId, memberIds: members, tasks: tasks.map((t) => ({ id: t.id, owner: t.owner, goal: t.goal, scopes: lines(t.scopes), status: "queued" as const })) })}>Kaynak HEAD önizlemesi al</Button>
        {preview && draftRoot === root && preview.projectRoot === root && <div className="grid gap-2 border-y border-border-w py-2"><b className="text-text2" style={{ fontSize: "var(--t-caption)" }}>Oluşturma önizlemesi · {preview.baseCommit}</b><p className="break-all text-muted" style={{ fontSize: "var(--t-caption)" }}>{preview.projectRoot} · {preview.sessionId} · {preview.targetVersion}<br />{preview.goal}</p>{preview.warnings.map((w, i) => <p key={i} className="text-warn" style={{ fontSize: "var(--t-caption)" }}>{w}</p>)}{preview.tasks.map((t) => <p key={t.id} className="text-text2" style={{ fontSize: "var(--t-caption)" }}>{t.id} · {t.owner} · {t.goal} · {t.scopes.join(", ")}</p>)}<p className="text-muted" style={{ fontSize: "var(--t-caption)" }}>Bu adım yalnızca bir kez metadata oluşturur; kaynak dosyaları değiştirmez.</p><Button size="sm" variant="primary" loading={busy} disabled={preview.projectRoot !== root || draftRoot !== root || busy} onClick={() => void create(preview.previewId)}>Yeni metadata oturumu oluştur</Button></div>}
      </>}
      {(!current || editing || status?.state === "creation_failed") && !otherRunning && mode === "existing" && <>
        <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Yerel bare store yolu<input className={input} maxLength={4096} value={storePath} onChange={(e) => setStorePath(e.target.value)} /></label><label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Yerel hub yolu<input className={input} maxLength={4096} value={hubPath} onChange={(e) => setHubPath(e.target.value)} /></label><label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Sahip kimliği<input className={input} maxLength={128} value={ownerId} onChange={(e) => setOwnerId(e.target.value)} /></label><label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Üye kimlikleri<input className={input} maxLength={32768} value={memberText} onChange={(e) => setMemberText(e.target.value)} /><span className="text-faint">Sahip kimliği de üye listesinde açıkça bulunmalıdır.</span></label><Button size="sm" loading={busy} disabled={busy || !storePath.trim() || !hubPath.trim() || !ownerId.trim() || !members.includes(ownerId) || !members.length || members.length > 256 || members.some((m) => m.length > 128)} onClick={() => void select({ storePath: storePath.trim(), hubPath: hubPath.trim(), ownerId, memberIds: members })}>Bu mevcut metadata oturumunu seç</Button>
      </>}
      {current && status && <div className="grid gap-2 border-y border-border-w py-2"><b className="text-text" style={{ fontSize: "var(--t-caption)" }}>Durum: {status.state}</b><p className="break-all text-muted" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{status.projectRoot}<br />Oturum: {status.sessionId} · sürüm: {status.targetVersion}<br />HEAD: {status.baseCommit} · rev: {status.revision}<br />Store: {status.storePath}<br />Hub: {status.hubPath}<br />Uç nokta: {status.endpoint}</p>{status.createdPaths.map((path) => <p key={path} className="break-all text-faint" style={{ fontSize: "var(--t-caption)" }}>Oluşturulan metadata: {path}</p>)}<p className="text-warn" style={{ fontSize: "var(--t-caption)" }}>Sunucu yalnız 127.0.0.1 üzerinde dinler; başka bilgisayardan erişim sağlamaz. Görev kapsamları danışma etiketidir, izin/kilit değildir. Başlatma ajan veya görev çalıştırmaz.</p>
        {status.state === "configured" || status.state === "stopped" ? !editing && <Button size="sm" loading={busy} onClick={() => void start()}>Loopback sunucusunu başlat</Button> : null}
        {running && !editing && <Button size="sm" loading={busy} onClick={() => void stopOwned()}>Sunucuyu durdur</Button>}
        {status.state === "cleanup_failed" && status.retryRequired && <Button size="sm" loading={busy} onClick={() => void stopOwned()}>Kapatmayı yeniden dene</Button>}
        {editing && <Button size="sm" disabled={busy} onClick={() => setEditing(false)}>Düzenlemeyi kapat</Button>}
        {running && !editing && <div className="grid gap-2 border-t border-border-w pt-2"><label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Bir kez erişim verilecek üye<select className={input} value={shareMember} onChange={(e) => { setShareMember(e.target.value); clearShare(); }}><option value="">Üye seçin</option>{membersForStatus.filter((m) => !status.exportedMembers.includes(m)).map((m) => <option key={m}>{m}</option>)}</select></label><label className="flex items-start gap-2 text-warn" style={{ fontSize: "var(--t-caption)" }}><input type="checkbox" checked={confirmSecret} onChange={(e) => setConfirmSecret(e.target.checked)} />Gizli erişim bilgisinin açıkça gösterilmesini ve yalnızca kendi sorumluluğumla paylaşmayı onaylıyorum.</label><Button size="sm" loading={sharing} disabled={!shareMember || !confirmSecret || busy || sharing} onClick={() => void showShare()}>Erişim bilgisini bir kez ver</Button>{share && <div className="grid gap-2"><p className="text-text2" style={{ fontSize: "var(--t-caption)" }}>Üye: {share.memberId} · görevler: {share.taskIds.join(", ")}<br />Uç nokta: {share.endpoint}<br />Kapsam: yalnız bu bilgisayarda loopback. Başka bilgisayar bağlanamaz.</p><label className="grid gap-1 text-warn" style={{ fontSize: "var(--t-caption)" }}>Gizli credential · yalnız burada, salt okunur<input className={input} type="password" readOnly value={share.credential} autoComplete="off" /></label><p className="text-muted" style={{ fontSize: "var(--t-caption)" }}>Kopyalama ikinci ve açık bir eylemdir; pano uygulama dışındadır ve otomatik temizlenemez.</p><Button size="sm" onClick={() => void copyShare()}>Erişim JSON'unu panoya kopyala</Button><Button size="sm" onClick={clearShare}>Gizli bilgiyi kapat</Button></div>}</div>}
        {running && !editing && <div className="grid gap-2 border-t border-border-w pt-2"><b className="text-text2" style={{ fontSize: "var(--t-caption)" }}>Bu uygulamada yerel görev önizlemesi</b><select className={input} value={localMember} onChange={(e) => { clearHandoff(); setLocalMember(e.target.value); setLocalTask(""); }}><option value="">Üye seçin</option>{membersForStatus.map((m) => <option key={m}>{m}</option>)}</select><select className={input} value={localTask} onChange={(e) => { clearHandoff(); setLocalTask(e.target.value); }}><option value="">Atanmış görev seçin</option>{status.tasks.filter((t) => t.owner === localMember && ["queued", "running", "waiting"].includes(t.status)).map((t) => <option key={t.id} value={t.id}>{t.id} · {t.goal}</option>)}</select><Button size="sm" loading={localPreviewBusy} disabled={!localMember || !localTask || localPreviewBusy || runStatus === "running" || runStage === "ready"} onClick={() => void loadLocalPreview()}>Görev önizlemesi al</Button>{handoff && <div className="grid gap-1 border-y border-border-w py-2"><p className="break-all text-text2" style={{ fontSize: "var(--t-caption)" }}>Önizleme (onay değildir): {handoff.preview.task.goal}<br />Üye {handoff.memberId} · görev {handoff.taskId} · rev {handoff.preview.revision}</p><label className="flex gap-2 text-muted" style={{ fontSize: "var(--t-caption)" }}><input type="checkbox" checked={handoffSeen} onChange={(e) => setHandoffSeen(e.target.checked)} />Görev önizlemesini gördüm.</label><Button size="sm" disabled={!canAdoptHandoff} onClick={adoptHandoff}>Bu uygulamada kullan · Ortak bağlam'a aktar</Button><p className="text-warn" style={{ fontSize: "var(--t-caption)" }}>Sıradaki adımda ortak bağlamı ayrıca inceleyip onaylamanız gerekir. Çalıştırma başlamaz.</p></div>}</div>}
      </div>}
    </> : <p className="text-muted" style={{ fontSize: "var(--t-caption)" }}>Önce bir proje açın.</p>}
    {copyNotice && <p role="status" className="text-warn" style={{ fontSize: "var(--t-caption)" }}>{copyNotice}</p>}
    {error && <p role="alert" className="text-err" style={{ fontSize: "var(--t-caption)" }}>{error}</p>}
  </section>;
}
