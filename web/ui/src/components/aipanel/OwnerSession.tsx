import { useEffect, useMemo, useRef, useState } from "react";
import { bridge, OwnerShare, ParticipantInvite } from "@/bridge";
import { Button } from "@/components/ui";
import { useCollaboration } from "@/state/collaboration";
import { useDelivery } from "@/state/delivery";
import { useOwner } from "@/state/owner";
import { useParticipant } from "@/state/participant";
import { useRun } from "@/state/run";
import { useWorkspace } from "@/state/workspace";
import { projectEpoch } from "@/state/projectEpoch";
import { PeerDelivery } from "./PeerDelivery";

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
  const startLAN = useOwner((s) => s.startLAN);
  const issueInvite = useOwner((s) => s.issueInvite);
  const revokeMember = useOwner((s) => s.revokeMember);
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
  const [lanAddress, setLanAddress] = useState("");
  const [certificatePath, setCertificatePath] = useState("");
  const [privateKeyPath, setPrivateKeyPath] = useState("");
  const [inviteMember, setInviteMember] = useState("");
  const [inviteBundle, setInviteBundle] = useState<Awaited<ReturnType<typeof issueInvite>>>(null);
  const [showInvite, setShowInvite] = useState(false);
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
  const [participantInviteText, setParticipantInviteText] = useState("");
  const [participantPin, setParticipantPin] = useState("");
  const [pinVerified, setPinVerified] = useState(false);
  const participant = useParticipant((s) => s.session);
  const participantMessage = useParticipant((s) => s.message);
  const participantBusy = useParticipant((s) => s.busy);
  const setParticipant = useParticipant((s) => s.setSession);
  const setParticipantMessage = useParticipant((s) => s.setMessage);
  const participantGeneration = useRef(0);
  const mounted = useRef(false);
  const localFlight = useRef(false);
  const shareFlight = useRef(false);
  const handoffGeneration = useRef(0);
  const handoffId = useRef<string | null>(null);
  const shareGeneration = useRef(0);
  const inviteGeneration = useRef(0);
  const inviteFlight = useRef(false);
  const inviteCopyFlight = useRef(false);
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
  const clearInvite = () => {
    inviteGeneration.current += 1;
    setInviteBundle(null); setShowInvite(false);
  };
  const stopOwned = async () => {
    clearShare(); clearHandoff(); clearInvite();
    return stop();
  };
  const startLanOwned = async () => {
    if (!root || !certificatePath.trim() || !privateKeyPath.trim()) return;
    clearInvite();
    await startLAN({ bindAddress: lanAddress.trim(), certificate: certificatePath.trim(), privateKey: privateKeyPath.trim() });
  };
  const createInvite = async () => {
    if (inviteFlight.current || !inviteMember || !current || !running || status?.transportMode !== "lan") return;
    clearInvite();
    const generation = inviteGeneration.current, epoch = projectEpoch(), capturedRoot = root;
    inviteFlight.current = true;
    try {
      const bundle = await issueInvite(inviteMember);
      if (!mounted.current || generation !== inviteGeneration.current || projectEpoch() !== epoch || useWorkspace.getState().root !== capturedRoot) {
        if (bundle && capturedRoot) void bridge.call("collab.owner.cancelInvite", { code: bundle.code, projectRoot: capturedRoot, expectedEpoch: bundle.epoch }).catch(() => undefined);
        return;
      }
      setInviteBundle(bundle); setShowInvite(false);
    } finally { inviteFlight.current = false; }
  };
  const participantCurrent = (generation: number, capturedRoot: string, epoch: number) =>
    mounted.current && generation === participantGeneration.current && projectEpoch() === epoch && useWorkspace.getState().root === capturedRoot;
  const pairParticipant = async () => {
    if (!root || !participantInviteText.trim() || !participantPin.trim() || !pinVerified || participant || participantBusy) return;
    let invite: ParticipantInvite;
    try {
      const parsed: unknown = JSON.parse(participantInviteText);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
      const candidate = parsed as Partial<ParticipantInvite>;
      if (typeof candidate.memberId !== "string" || typeof candidate.code !== "string" || typeof candidate.sessionId !== "string" ||
          typeof candidate.controlEndpoint !== "string" || typeof candidate.proposalEndpoint !== "string" || typeof candidate.certificateSha256 !== "string" ||
          (candidate.expiresInSeconds !== undefined && typeof candidate.expiresInSeconds !== "number") ||
          (candidate.epoch !== undefined && typeof candidate.epoch !== "number")) throw new Error();
      invite = candidate as ParticipantInvite;
    } catch { setParticipantMessage("Davet paketi geçersiz; daveti yeniden yapıştırın."); return; }
    const capturedRoot = root, epoch = projectEpoch(), generation = ++participantGeneration.current;
    const flight = useParticipant.getState().begin();
    if (flight === null) return;
    try {
      if (participantPin.trim().toLowerCase() !== invite.certificateSha256) {
        setParticipantMessage("PIN davetteki parmak iziyle eşleşmiyor; ayrı kanaldan doğrulayın."); return;
      }
      const pending = bridge.call("collab.peer.join", { bundle: invite, confirmPin: true, pin: participantPin.trim().toLowerCase() });
      setParticipantInviteText(""); setParticipantPin(""); setPinVerified(false);
      const result = await pending;
      if (!participantCurrent(generation, capturedRoot, epoch) || result.projectRoot !== capturedRoot) {
        if (result.peerHandle) void bridge.call("collab.peer.disconnect", { peerHandle: result.peerHandle }).catch(() => undefined);
        return;
      }
      setParticipant(result);
      setParticipantMessage("Katılımcı eşleştirildi.");
    } catch {
      if (participantCurrent(generation, capturedRoot, epoch)) setParticipantMessage("Eşleştirmenin sonucu belirsiz olabilir. Daveti veya PIN'i yeniden kullanmayın; sahibinden durumu doğrulamasını isteyin.");
    } finally { useParticipant.getState().finish(flight); }
  };
  const refreshParticipant = async () => {
    if (!root || participantBusy) return;
    const flight = useParticipant.getState().begin();
    if (flight === null) return;
    const capturedRoot = root, epoch = projectEpoch(), generation = ++participantGeneration.current;
    try {
      const current = participant;
      if (!current || current.projectRoot !== capturedRoot) return;
      const next = await bridge.call("collab.peer.refresh", { peerHandle: current.peerHandle });
      if (participantCurrent(generation, capturedRoot, epoch)) {
        setParticipant(next.projectRoot === capturedRoot ? next : null);
        setParticipantMessage(next.state === "active" ? "Katılımcı metadata yenilendi." : "Durum belirsiz; kullanılabilir metadata temizlendi.");
      }
    } catch {
      if (participantCurrent(generation, capturedRoot, epoch)) {
        const current = participant;
        if (current) setParticipant({ peerHandle: current.peerHandle, projectRoot: current.projectRoot,
          sessionId: current.sessionId, memberId: current.memberId, epoch: current.epoch,
          state: "unknown", error: "refresh_failed" });
        setParticipantMessage("Yenileme başarısız; kullanılabilir metadata temizlendi. Sahipten erişimi iptal etmesini isteyin.");
      }
    }
    finally { useParticipant.getState().finish(flight); }
  };
  const disconnectParticipant = async () => {
    if (!root || !participant || participantBusy || participant.projectRoot !== root) return;
    const flight = useParticipant.getState().begin();
    if (flight === null) return;
    const capturedRoot = root, epoch = projectEpoch(), generation = ++participantGeneration.current;
    try {
      const result = await bridge.call("collab.peer.disconnect", { peerHandle: participant.peerHandle });
      if (participantCurrent(generation, capturedRoot, epoch)) { setParticipant(null); setParticipantMessage(result.warning || "Katılımcı bağlantısı kesildi."); }
    } catch { if (participantCurrent(generation, capturedRoot, epoch)) setParticipantMessage("Bağlantı kesilemedi."); }
    finally { useParticipant.getState().finish(flight); }
  };
  const copyInvite = async () => {
    const bundle = inviteBundle, generation = inviteGeneration.current, epoch = projectEpoch();
    if (!bundle || !showInvite || inviteCopyFlight.current || useWorkspace.getState().root !== root) return;
    const live = useOwner.getState().status;
    if (live?.epoch !== bundle.epoch || live.projectRoot !== root || live.state !== "running") return;
    inviteCopyFlight.current = true;
    try {
      await bridge.call("app.clipboardWrite", { text: JSON.stringify(bundle) });
      if (mounted.current && projectEpoch() === epoch && inviteGeneration.current === generation) {
        clearInvite(); setCopyNotice("Davet panoya kopyalandı; panoyu güvenli biçimde temizleyin.");
      }
    } catch {
      if (mounted.current && projectEpoch() === epoch && inviteGeneration.current === generation) setCopyNotice("Davet kopyalanamadı; kodu paylaşmayın.");
    } finally { inviteCopyFlight.current = false; }
  };

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, 1000);
    return () => {
      mounted.current = false;
      setParticipantInviteText(""); setParticipantPin(""); setPinVerified(false);
      inviteGeneration.current += 1;
      participantGeneration.current += 1;
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
    participantGeneration.current += 1;
    useParticipant.getState().resetRoot(root);
    setParticipantInviteText(""); setParticipantPin(""); setPinVerified(false);
    clearShare(); clearHandoff(); setEditing(false);
    setMode("create"); setSessionId("imece-session"); setVersion("v1"); setGoal("");
    setOwnerId("owner"); setMemberText("owner\nmember-1"); setTasks(initialTasks);
    setStorePath(""); setHubPath("");
    setLanAddress(""); setCertificatePath(""); setPrivateKeyPath("");
    clearInvite(); setInviteMember("");
    setLocalMember(""); setLocalTask("");
  }, [root]);
  useEffect(() => {
    if (share && (status?.epoch !== share.epoch || !running || status.projectRoot !== root)) clearShare();
    if (inviteBundle && (status?.epoch !== inviteBundle.epoch || !running || status.projectRoot !== root)) clearInvite();
    if (handoff && (!running || status?.epoch !== handoff.epoch || status.projectRoot !== root || runStatus === "running" || runStage === "ready")) clearHandoff();
  }, [status?.epoch, status?.projectRoot, running, root, share, inviteBundle, handoff, runStatus, runStage]);
  useEffect(() => {
    if (!inviteBundle) return;
    const timer = window.setTimeout(clearInvite, (inviteBundle.expiresInSeconds ?? 300) * 1000);
    return () => window.clearTimeout(timer);
  }, [inviteBundle]);

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
  let participantFingerprint = "";
  try {
    const pasted: unknown = JSON.parse(participantInviteText);
    if (pasted && typeof pasted === "object" && "certificateSha256" in pasted && typeof pasted.certificateSha256 === "string") participantFingerprint = pasted.certificateSha256;
  } catch { /* incomplete paste */ }

  const editConfiguration = async () => {
    if (!current || !status || serverLocked || busy) return;
    if (status.state === "creation_failed") { setEditing(true); return; }
    const stopped = await stop();
    const latest = useOwner.getState().status;
    if (stopped && latest?.projectRoot === root && latest.state === "stopped") setEditing(true);
  };

  return <section className="flex h-full flex-col gap-3 overflow-y-auto px-3 py-3" aria-label="Sahip oturumu">
    <div><h2 className="text-text" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>Yerel sahip oturumu</h2><p className="mt-1 text-muted" style={{ fontSize: "var(--t-caption)", lineHeight: 1.45 }}>Yerel metadata, açıkça başlatılan sunucu ve katılımcı erişimi. Katılım görev çalıştırmaz; kod yalnız ayrı seçim ve paylaşım onayıyla aktarılır.</p></div>
    {!bridge.isNative && <p className="text-warn" style={{ fontSize: "var(--t-caption)" }}>MOCK: dosya sistemi, listener ve gerçek kimlik bilgisi yok; erişim bilgisi simülasyondur.</p>}
    {root && <details className="border-y border-border-w py-2"><summary className="cursor-pointer text-text2" style={{ fontSize: "var(--t-caption)" }}>Katılımcı oturumuna katıl</summary>
      <p className="my-2 text-muted" style={{ fontSize: "var(--t-caption)" }}>Davet kodunu güvenli kanaldan yapıştırın. Parmak izini davet edenden ayrı kanalda doğrulayıp PIN'i girin. Kod ve PIN yalnızca geçici bellekte tutulur; kaydedilmez veya loglanmaz.</p>
      <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Davet paketi<textarea className={input} maxLength={8192} value={participantInviteText} autoComplete="off" onChange={(e) => { setParticipantInviteText(e.target.value); setPinVerified(false); setParticipantMessage(""); }} /></label>
      {participantFingerprint && <p className="break-all text-text2" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>Davet parmak izi SHA-256: {participantFingerprint}</p>}
      <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Ayrı kanaldan doğrulanan PIN<input className={input} type="password" maxLength={128} value={participantPin} autoComplete="new-password" onChange={(e) => { setParticipantPin(e.target.value); setPinVerified(false); }} /></label>
      <label className="my-2 flex items-start gap-2 text-warn" style={{ fontSize: "var(--t-caption)" }}><input type="checkbox" checked={pinVerified} onChange={(e) => setPinVerified(e.target.checked)} />Sertifika SHA-256 parmak izini ayrı güvenlik kanalından doğruladım.</label>
      <div className="flex flex-wrap gap-2"><Button size="sm" loading={participantBusy} disabled={participantBusy || !!participant || !participantInviteText.trim() || !participantPin.trim() || !pinVerified} onClick={() => void pairParticipant()}>Eşleştir</Button><Button size="sm" variant="secondary" disabled={participantBusy || !participant} loading={participantBusy} onClick={() => void refreshParticipant()}>Durumu yenile</Button>{participant && <Button size="sm" variant="secondary" disabled={participantBusy} loading={participantBusy} onClick={() => void disconnectParticipant()}>Bağlantıyı kes</Button>}</div>
      {participant && <div className="mt-2 grid gap-1 text-text2" style={{ fontSize: "var(--t-caption)" }}><p>Durum: {participant.state} · {participant.memberId} · {participant.sessionId}</p>{participant.context && <><p><b>Ortak hedef:</b> {participant.context.goal}</p><p><b>Kararlar:</b> {participant.context.decisions.join(" · ") || "—"}</p><p><b>Arayüzler:</b> {Object.entries(participant.context.interfaces).map(([name, value]) => `${name}: ${value}`).join(" · ") || "—"}</p></>}{participant.tasks?.map((task) => <p key={task.id}><b>{task.id}</b> · {task.owner} · {task.status} · {task.goal} · {task.scopes.join(", ")}</p>)}</div>}
      {participantMessage && <p role="status" className="mt-2 text-muted" style={{ fontSize: "var(--t-caption)" }}>{participantMessage}</p>}
      <PeerDelivery />
    </details>}
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
      {current && status && <div className="grid gap-2 border-y border-border-w py-2"><b className="text-text" style={{ fontSize: "var(--t-caption)" }}>Durum: {status.state}</b><p className="break-all text-muted" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>{status.projectRoot}<br />Oturum: {status.sessionId}<br />{status.transportMode === "lan" ? <>Kontrol TLS: {status.controlEndpoint}<br />Öneri TLS: {status.proposalEndpoint}<br />Sertifika SHA-256: {status.certificateSha256}</> : <>Sürüm: {status.targetVersion}<br />HEAD: {status.baseCommit} · rev: {status.revision}<br />Store: {status.storePath}<br />Hub: {status.hubPath}<br />Uç nokta: {status.endpoint}</>}</p>{status.createdPaths.map((path) => <p key={path} className="break-all text-faint" style={{ fontSize: "var(--t-caption)" }}>Oluşturulan metadata: {path}</p>)}<p className="text-warn" style={{ fontSize: "var(--t-caption)" }}>{status.transportMode === "lan" ? "TLS LAN açık. Davet kodunu yalnız seçtiğiniz kişiyle güvenli kanaldan paylaşın; sertifika parmak izini ayrı doğrulayın. Görev kapsamları izin/kilit değildir." : "Sunucu yalnız 127.0.0.1 üzerinde dinler; başka bilgisayardan erişim sağlamaz."} Başlatma ajan veya görev çalıştırmaz.</p>
        {(status.state === "configured" || status.state === "stopped") && !editing && <div className="grid gap-2">
          <Button size="sm" loading={busy} onClick={() => void start()}>Yalnız bu bilgisayarda başlat</Button>
          <details className="border-t border-border-w pt-2"><summary className="cursor-pointer text-text2" style={{ fontSize: "var(--t-caption)" }}>LAN TLS sunucusunu açıkça yapılandır</summary>
            <p className="my-2 text-warn" style={{ fontSize: "var(--t-caption)" }}>Özel ağda dinler. Yalnız daveti verdiğiniz kişiler erişebilir. Öneri TLS kanalı ayrı portta açılır. Sertifika güvenini parmak iziyle cihazlar arasında ayrıca doğrulayın.</p>
            <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Özel IPv4 adresi<input className={input} value={lanAddress} onChange={(e) => setLanAddress(e.target.value)} autoComplete="off" /></label>
            <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>TLS sertifika PEM yolu<input className={input} value={certificatePath} onChange={(e) => setCertificatePath(e.target.value)} autoComplete="off" /></label>
            <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>TLS özel anahtar PEM yolu<input className={input} type="password" value={privateKeyPath} onChange={(e) => setPrivateKeyPath(e.target.value)} autoComplete="new-password" /></label>
            <Button size="sm" loading={busy} disabled={!lanAddress.trim() || !certificatePath.trim() || !privateKeyPath.trim() || busy} onClick={() => void startLanOwned()}>İki TLS listener’ı başlat</Button>
          </details>
        </div>}
        {running && !editing && <Button size="sm" loading={busy} onClick={() => void stopOwned()}>Sunucuyu durdur</Button>}
        {running && status.transportMode === "lan" && !editing && <div className="grid gap-2 border-t border-border-w pt-2">
          <label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Davet edilecek yapılandırılmış üye<select className={input} value={inviteMember} onChange={(e) => { setInviteMember(e.target.value); clearInvite(); }}><option value="">Üye seçin</option>{status.memberIds.filter((m) => m !== status.ownerId).map((m) => <option key={m} value={m}>{m}</option>)}</select></label>
          <Button size="sm" loading={busy} disabled={!inviteMember || busy} onClick={() => void createInvite()}>Tek kullanımlık davet üret</Button>
          {inviteBundle && <div className="grid gap-2 border-y border-warn/40 py-2"><p className="text-warn" style={{ fontSize: "var(--t-caption)" }}>Davet kodu {inviteBundle.expiresInSeconds} saniye geçerli ve bir kez kullanılabilir. Yalnız {inviteBundle.memberId} ile güvenli biçimde paylaşın; kontrol/öneri adreslerini ve parmak izini de iletin.</p><label className="flex items-start gap-2 text-muted" style={{ fontSize: "var(--t-caption)" }}><input type="checkbox" checked={showInvite} onChange={(e) => setShowInvite(e.target.checked)} />Davet bilgisini açıkça gösterip paylaşmayı onaylıyorum.</label>{showInvite && <><input className={input} type="text" readOnly value={JSON.stringify(inviteBundle)} autoComplete="off" /><Button size="sm" onClick={() => void copyInvite()}>Davet paketini panoya kopyala</Button></>}</div>}
          {status.memberIds.filter((m) => m !== status.ownerId).map((member) => <Button key={member} size="sm" variant="secondary" disabled={busy} onClick={() => { clearInvite(); void revokeMember(member); }}>Üyeyi iptal et: {member}</Button>)}
        </div>}
        {status.state === "cleanup_failed" && status.retryRequired && <Button size="sm" loading={busy} onClick={() => void stopOwned()}>Kapatmayı yeniden dene</Button>}
        {editing && <Button size="sm" disabled={busy} onClick={() => setEditing(false)}>Düzenlemeyi kapat</Button>}
        {running && status.transportMode !== "lan" && !editing && <div className="grid gap-2 border-t border-border-w pt-2"><label className="grid gap-1 text-muted" style={{ fontSize: "var(--t-caption)" }}>Bir kez erişim verilecek üye<select className={input} value={shareMember} onChange={(e) => { setShareMember(e.target.value); clearShare(); }}><option value="">Üye seçin</option>{membersForStatus.filter((m) => !status.exportedMembers.includes(m)).map((m) => <option key={m}>{m}</option>)}</select></label><label className="flex items-start gap-2 text-warn" style={{ fontSize: "var(--t-caption)" }}><input type="checkbox" checked={confirmSecret} onChange={(e) => setConfirmSecret(e.target.checked)} />Gizli erişim bilgisinin açıkça gösterilmesini ve yalnızca kendi sorumluluğumla paylaşmayı onaylıyorum.</label><Button size="sm" loading={sharing} disabled={!shareMember || !confirmSecret || busy || sharing} onClick={() => void showShare()}>Erişim bilgisini bir kez ver</Button>{share && <div className="grid gap-2"><p className="text-text2" style={{ fontSize: "var(--t-caption)" }}>Üye: {share.memberId} · görevler: {share.taskIds.join(", ")}<br />Uç nokta: {share.endpoint}<br />Kapsam: yalnız bu bilgisayarda loopback. Başka bilgisayar bağlanamaz.</p><label className="grid gap-1 text-warn" style={{ fontSize: "var(--t-caption)" }}>Gizli credential · yalnız burada, salt okunur<input className={input} type="password" readOnly value={share.credential} autoComplete="off" /></label><p className="text-muted" style={{ fontSize: "var(--t-caption)" }}>Kopyalama ikinci ve açık bir eylemdir; pano uygulama dışındadır ve otomatik temizlenemez.</p><Button size="sm" onClick={() => void copyShare()}>Erişim JSON'unu panoya kopyala</Button><Button size="sm" onClick={clearShare}>Gizli bilgiyi kapat</Button></div>}</div>}
        {running && status.transportMode !== "lan" && !editing && <div className="grid gap-2 border-t border-border-w pt-2"><b className="text-text2" style={{ fontSize: "var(--t-caption)" }}>Bu uygulamada yerel görev önizlemesi</b><select className={input} value={localMember} onChange={(e) => { clearHandoff(); setLocalMember(e.target.value); setLocalTask(""); }}><option value="">Üye seçin</option>{membersForStatus.map((m) => <option key={m}>{m}</option>)}</select><select className={input} value={localTask} onChange={(e) => { clearHandoff(); setLocalTask(e.target.value); }}><option value="">Atanmış görev seçin</option>{status.tasks.filter((t) => t.owner === localMember && ["queued", "running", "waiting"].includes(t.status)).map((t) => <option key={t.id} value={t.id}>{t.id} · {t.goal}</option>)}</select><Button size="sm" loading={localPreviewBusy} disabled={!localMember || !localTask || localPreviewBusy || runStatus === "running" || runStage === "ready"} onClick={() => void loadLocalPreview()}>Görev önizlemesi al</Button>{handoff && <div className="grid gap-1 border-y border-border-w py-2"><p className="break-all text-text2" style={{ fontSize: "var(--t-caption)" }}>Önizleme (onay değildir): {handoff.preview.task.goal}<br />Üye {handoff.memberId} · görev {handoff.taskId} · rev {handoff.preview.revision}</p><label className="flex gap-2 text-muted" style={{ fontSize: "var(--t-caption)" }}><input type="checkbox" checked={handoffSeen} onChange={(e) => setHandoffSeen(e.target.checked)} />Görev önizlemesini gördüm.</label><Button size="sm" disabled={!canAdoptHandoff} onClick={adoptHandoff}>Bu uygulamada kullan · Ortak bağlam'a aktar</Button><p className="text-warn" style={{ fontSize: "var(--t-caption)" }}>Sıradaki adımda ortak bağlamı ayrıca inceleyip onaylamanız gerekir. Çalıştırma başlamaz.</p></div>}</div>}
      </div>}
    </> : <p className="text-muted" style={{ fontSize: "var(--t-caption)" }}>Önce bir proje açın.</p>}
    {copyNotice && <p role="status" className="text-warn" style={{ fontSize: "var(--t-caption)" }}>{copyNotice}</p>}
    {error && <p role="alert" className="text-err" style={{ fontSize: "var(--t-caption)" }}>{error}</p>}
  </section>;
}
