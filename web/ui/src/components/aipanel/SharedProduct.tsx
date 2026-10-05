import { useCallback, useEffect, useRef, useState } from "react";
import { bridge, BridgeError, ProductBoard, ProductChangeEvent, ProductChanges, ProductProposals } from "@/bridge";
import { Button } from "@/components/ui";
import { ProductHistory } from "./ProductHistory";
import { CreateSharedTask } from "./CreateSharedTask";
import { useOwner } from "@/state/owner";
import { useDelivery } from "@/state/delivery";
import { useRun } from "@/state/run";
import { useWorkspace } from "@/state/workspace";

const input = "min-w-0 w-full rounded-[var(--r-sm)] border border-border-w bg-field px-2 py-1.5 text-text outline-none focus-visible:border-accent";
type TaskStatus = ProductBoard["tasks"][number]["status"];
type TaskConsent = Record<string, { revision: string; status: TaskStatus }>;

function validHistoryPage(value: ProductChanges, captured: ProductBoard, after: string): boolean {
  const boundedId = (item: unknown): item is string => typeof item === "string" && item.length > 0 && item.length <= 128;
  if (!value || value.sessionId !== captured.sessionId || value.baseCommit !== captured.baseCommit ||
      value.epoch !== captured.epoch || !boundedId(value.headRevision) ||
      !boundedId(value.lastRevision) || typeof value.hasMore !== "boolean" ||
      !Array.isArray(value.events) || value.events.length > 16) return false;
  let expected = after;
  for (const event of value.events) {
    if (!event || event.fromRevision !== expected || !boundedId(event.toRevision) || event.toRevision === expected ||
        typeof event.metadataCheckpoint !== "boolean" || typeof event.goalChanged !== "boolean" ||
        typeof event.decisionsChanged !== "boolean" || typeof event.contextChanged !== "boolean" ||
        !event.interfaces || ![event.interfaces.added, event.interfaces.removed, event.interfaces.changed].every((list) => Array.isArray(list) && list.length <= 64 && list.every(boundedId)) ||
        !Array.isArray(event.taskChanges) || event.taskChanges.length > 32 ||
        !Number.isInteger(event.taskChangeCount) || event.taskChangeCount < event.taskChanges.length ||
        typeof event.taskChangesTruncated !== "boolean" || event.taskChangesTruncated !== (event.taskChangeCount > event.taskChanges.length) ||
        !Array.isArray(event.affectedTaskIds) || event.affectedTaskIds.length > 32 || !event.affectedTaskIds.every(boundedId) ||
        !Number.isInteger(event.affectedTaskCount) || event.affectedTaskCount < event.affectedTaskIds.length ||
        typeof event.affectedTasksTruncated !== "boolean" || event.affectedTasksTruncated !== (event.affectedTaskCount > event.affectedTaskIds.length)) return false;
    for (const task of event.taskChanges) {
      if (!task || !boundedId(task.taskId) || !["added", "removed", "changed"].includes(task.change) ||
          !Array.isArray(task.fields) || task.fields.length > 8 || !task.fields.every((field) => ["goal", "assignment", "scopes", "contextRevision", "status"].includes(field)) ||
          ![task.previousStatus, task.status].every((status) => status === null || ["queued", "running", "waiting", "done"].includes(status)) ||
          ![task.previousOwner, task.owner].every((owner) => owner === null || boundedId(owner))) return false;
    }
    expected = event.toRevision;
  }
  return value.lastRevision === expected && (value.events.length > 0 || value.lastRevision === after) &&
    (value.hasMore ? value.events.length > 0 && value.lastRevision !== value.headRevision : value.lastRevision === value.headRevision);
}

export function SharedProduct({ onNavigate }: { onNavigate: (tab: "owner" | "shared") => void }) {
  const root = useWorkspace((s) => s.root);
  const owner = useOwner((s) => s.status);
  const candidate = useDelivery((s) => s.candidate);
  const deliveryRoot = useDelivery((s) => s.root);
  const deliveryRun = useDelivery((s) => s.runId);
  const runId = useRun((s) => s.runId);
  const deliveryStore = useDelivery((s) => s.storePath);
  const deliveryHub = useDelivery((s) => s.hubPath);
  const deliveryConflicts = useDelivery((s) => s.conflicts);
  const [board, setBoard] = useState<ProductBoard | null>(null);
  const [error, setError] = useState("");
  const [fresh, setFresh] = useState(false);
  const [checkedAt, setCheckedAt] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [goal, setGoal] = useState("");
  const [decisions, setDecisions] = useState("[]");
  const [interfaces, setInterfaces] = useState("{}");
  const [consent, setConsent] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [taskChoice, setTaskChoice] = useState<Record<string, TaskStatus>>({});
  const [taskConsent, setTaskConsent] = useState<TaskConsent>({});
  const [proposals, setProposals] = useState<ProductProposals | null>(null);
  const [proposalsBusy, setProposalsBusy] = useState(false);
  const [writeBusy, setWriteBusy] = useState(false);
  const [changes, setChanges] = useState<ProductChangeEvent[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyAnchor, setHistoryAnchor] = useState<string | null>(null);
  const [historyMore, setHistoryMore] = useState(false);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [historyDropped, setHistoryDropped] = useState(0);
  const [historyResetAvailable, setHistoryResetAvailable] = useState(false);
  const generation = useRef(0);
  const mounted = useRef(false);
  const identityRef = useRef("");
  const dirtyRef = useRef(false);
  const boardRef = useRef<ProductBoard | null>(null);
  const draftRevision = useRef<string | null>(null);
  const flight = useRef(false);
  const operation = useRef(false);
  const proposalsFlight = useRef(false);
  const historyFlight = useRef(false);
  const historyId = useRef(0);
  const operationId = useRef(0);
  const proposalId = useRef(0);
  const fetchId = useRef(0);
  const currentIdentity = `${root ?? ""}|${owner?.sessionId ?? ""}|${owner?.epoch ?? -1}`;

  const clearSession = useCallback(() => {
    operationId.current++; proposalId.current++; historyId.current++; operation.current = false; proposalsFlight.current = false; historyFlight.current = false;
    setWriteBusy(false); setProposalsBusy(false);
    boardRef.current = null; setBoard(null); setError(""); setFresh(false); setCheckedAt(null);
    setGoal(""); setDecisions("[]"); setInterfaces("{}"); setDirty(false); dirtyRef.current = false;
    draftRevision.current = null; setConsent(false); setTaskChoice({}); setTaskConsent({}); setProposals(null);
    setChanges([]); setHistoryCursor(null); setHistoryAnchor(null); setHistoryMore(false); setHistoryError(""); setHistoryBusy(false); setHistoryDropped(0); setHistoryResetAvailable(false);
  }, []);

  const refresh = useCallback(async () => {
    const capturedRoot = useWorkspace.getState().root;
    if (!capturedRoot || !mounted.current || flight.current) return;
    const token = generation.current;
    const request = ++fetchId.current;
    const requestedIdentity = identityRef.current;
    flight.current = true; setLoading(true); setError("");
    try {
      const next = await bridge.call("collab.owner.snapshot", {});
      if (!mounted.current || request !== fetchId.current || token !== generation.current || useWorkspace.getState().root !== capturedRoot || next.projectRoot !== capturedRoot || identityRef.current !== requestedIdentity) return;
      const ownerNow = useOwner.getState().status;
      if (!ownerNow || ownerNow.projectRoot !== capturedRoot || ownerNow.sessionId !== next.sessionId || ownerNow.epoch !== next.epoch) {
        if (mounted.current && token === generation.current && useWorkspace.getState().root === capturedRoot) {
          setFresh(false); setError("Sahip oturum kimliği/dönemi değişmiş olabilir; Oturum sekmesinden durumu yenileyin.");
        }
        return;
      }
      const previous = boardRef.current;
      if (previous && (previous.sessionId !== next.sessionId || previous.epoch !== next.epoch)) { clearSession(); }
      else if (previous && previous.revision !== next.revision) { setConsent(false); setTaskConsent({}); setProposals(null); }
      boardRef.current = next; setBoard(next); setFresh(true); setCheckedAt(Date.now()); setError("");
      setHistoryAnchor((anchor) => anchor ?? next.revision);
      setTaskChoice((old) => Object.fromEntries(next.tasks.map((task) => [task.id,
        Object.prototype.hasOwnProperty.call(old, task.id) ? old[task.id] : task.status])));
      if (!dirtyRef.current) {
        draftRevision.current = next.revision;
        setGoal(next.context.goal); setDecisions(JSON.stringify(next.context.decisions, null, 2));
        setInterfaces(JSON.stringify(next.context.interfaces, null, 2));
      }
    } catch {
      if (mounted.current && request === fetchId.current && token === generation.current && useWorkspace.getState().root === capturedRoot && identityRef.current === requestedIdentity) {
        setFresh(false); setError("Ortak ürün panosu güncel okunamadı. Yazma ve doğrulama duraklatıldı; yeniden deneyin.");
      }
    } finally {
      if (request === fetchId.current) { flight.current = false; if (mounted.current && token === generation.current) setLoading(false); }
    }
  }, [clearSession]);

  useEffect(() => {
    mounted.current = true;
    identityRef.current = currentIdentity;
    generation.current++;
    clearSession();
    void useOwner.getState().refresh();
    void refresh();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void refresh(); }, 5000);
    const onVisible = () => { if (document.visibilityState === "visible") void refresh(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { mounted.current = false; generation.current++; operationId.current++; proposalId.current++; historyId.current++; fetchId.current++; flight.current = false; operation.current = false; proposalsFlight.current = false; historyFlight.current = false; window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [currentIdentity, clearSession, refresh]);

  const identityMatches = (captured: ProductBoard, token: number) => mounted.current && token === generation.current &&
    useWorkspace.getState().root === captured.projectRoot && identityRef.current === `${captured.projectRoot}|${captured.sessionId}|${captured.epoch}` &&
    (() => { const s = useOwner.getState().status; return !!s && s.projectRoot === captured.projectRoot && s.sessionId === captured.sessionId && s.epoch === captured.epoch; })();
  const edit = () => { if (!dirtyRef.current && boardRef.current) draftRevision.current = boardRef.current.revision; dirtyRef.current = true; setDirty(true); setConsent(false); };
  const resetDraft = (next: ProductBoard) => {
    dirtyRef.current = false; setDirty(false); draftRevision.current = next.revision; setGoal(next.context.goal);
    setDecisions(JSON.stringify(next.context.decisions, null, 2)); setInterfaces(JSON.stringify(next.context.interfaces, null, 2)); setConsent(false);
  };
  const stale = !!board && (!fresh || (!!draftRevision.current && draftRevision.current !== board.revision));

  const writeContext = async () => {
    const captured = boardRef.current;
    if (!captured || !fresh || !consent || !dirtyRef.current || stale || captured.state !== "running" || operation.current) return;
    let parsedDecisions: unknown, parsedInterfaces: unknown;
    try { parsedDecisions = JSON.parse(decisions); parsedInterfaces = JSON.parse(interfaces); }
    catch { setError("Kararlar ve arayüzler geçerli JSON olmalıdır."); return; }
    if (!Array.isArray(parsedDecisions) || parsedDecisions.some((v) => typeof v !== "string") || !parsedInterfaces || Array.isArray(parsedInterfaces) || typeof parsedInterfaces !== "object" || Object.values(parsedInterfaces).some((v) => typeof v !== "string")) { setError("Kararlar metin dizisi, arayüzler metin değerli JSON nesnesi olmalıdır."); return; }
    const token = generation.current, op = ++operationId.current;
    operation.current = true; setWriteBusy(true); setConsent(false); setTaskConsent({}); setError("");
    try {
      await bridge.call("collab.owner.updateContext", { confirm: true, expectedRevision: draftRevision.current ?? captured.revision, expectedEpoch: captured.epoch, expectedSessionId: captured.sessionId, context: { goal, decisions: parsedDecisions as string[], interfaces: parsedInterfaces as Record<string, string> } });
      if (!identityMatches(captured, token)) return;
      dirtyRef.current = false; setDirty(false); draftRevision.current = null; setProposals(null);
      // A pre-commit poll must not restore the old context after this receipt.
      fetchId.current++; flight.current = false; setFresh(false);
      await refresh();
    } catch {
      if (identityMatches(captured, token)) { setFresh(false); setError("Güncelleme reddedildi. Taslak korundu; panoyu yenileyip yeniden inceleyin."); }
    } finally {
      if (op === operationId.current) { operation.current = false; if (mounted.current && token === generation.current) setWriteBusy(false); }
    }
  };

  const updateTask = async (task: ProductBoard["tasks"][number]) => {
    const captured = boardRef.current, selected = taskChoice[task.id];
    const approved = taskConsent[task.id];
    if (!captured || !fresh || !approved || approved.revision !== captured.revision || approved.status !== selected || !selected || selected === task.status || captured.state !== "running" || operation.current) return;
    const token = generation.current, op = ++operationId.current;
    operation.current = true; setWriteBusy(true); setTaskConsent({}); setConsent(false); setError("");
    try {
      await bridge.call("collab.owner.updateTaskStatus", { confirm: true, expectedRevision: approved.revision, expectedEpoch: captured.epoch, expectedSessionId: captured.sessionId, taskId: task.id, status: selected });
      if (!identityMatches(captured, token)) return;
      fetchId.current++; flight.current = false; setFresh(false);
      await refresh();
    } catch { if (identityMatches(captured, token)) { setFresh(false); setError("Görev güncellenemedi; panoyu yenileyip yeniden inceleyin."); } }
    finally { if (op === operationId.current) { operation.current = false; if (mounted.current && token === generation.current) setWriteBusy(false); } }
  };

  const createTask = async (expectedRevision: string, task: { id: string; owner: string; goal: string; scopes: string[] }): Promise<boolean> => {
    const captured = boardRef.current;
    if (!captured || !fresh || !identityMatches(captured, generation.current) || captured.state !== "running" || operation.current) return false;
    const token = generation.current, op = ++operationId.current;
    operation.current = true; setWriteBusy(true); setConsent(false); setTaskConsent({}); setError("");
    try {
      const receipt = await bridge.call("collab.owner.createTask", { confirm: true, expectedRevision, expectedEpoch: captured.epoch, expectedSessionId: captured.sessionId, task });
      if (!identityMatches(captured, token)) return false;
      if (!receipt || receipt.action !== "createTask" || receipt.sessionId !== captured.sessionId ||
          receipt.epoch !== captured.epoch || receipt.taskId !== task.id || receipt.owner !== task.owner ||
          receipt.status !== "queued" || receipt.contextRevision !== expectedRevision ||
          typeof receipt.revision !== "string" || !(bridge.isNative ? /^[0-9a-f]{40}$/ : /^mock-r[1-9][0-9]*$/).test(receipt.revision)) {
        setFresh(false); setError("Oluşturma yanıtı doğrulanamadı; görev eklenmiş olabilir. Panoyu ve bu kimliği kontrol edin; otomatik tekrar yok.");
        return false;
      }
      fetchId.current++; flight.current = false; setFresh(false);
      await refresh();
      return identityMatches(captured, token);
    } catch {
      if (identityMatches(captured, token)) { setFresh(false); setError("Yeni görev oluşturma onaylanamadı; taslak korundu. Panoyu ve görev kimliğini kontrol edin; otomatik tekrar yok."); }
      return false;
    } finally { if (op === operationId.current) { operation.current = false; if (mounted.current && token === generation.current) setWriteBusy(false); } }
  };

  const loadProposals = async () => {
    const captured = boardRef.current;
    if (!captured || !fresh || proposalsFlight.current) return;
    const token = generation.current, id = ++proposalId.current;
    proposalsFlight.current = true; setProposalsBusy(true); setError("");
    try {
      const value = await bridge.call("collab.owner.proposals", { expectedSessionId: captured.sessionId });
      if (id === proposalId.current && identityMatches(captured, token) && value.sessionId === captured.sessionId && value.epoch === captured.epoch) setProposals(value);
    } catch { if (id === proposalId.current && identityMatches(captured, token)) setError("Teklif metadata'sı yüklenemedi."); }
    finally { if (id === proposalId.current) { proposalsFlight.current = false; if (mounted.current && token === generation.current) setProposalsBusy(false); } }
  };

  const loadChanges = async () => {
    const captured = boardRef.current, anchor = historyCursor ?? historyAnchor;
    if (!captured || !fresh || !anchor || historyFlight.current) return;
    const token = generation.current, id = ++historyId.current;
    historyFlight.current = true; setHistoryBusy(true); setHistoryError("");
    try {
      const page = await bridge.call("collab.owner.changes", { expectedSessionId: captured.sessionId, afterRevision: anchor, limit: 16 });
      if (id !== historyId.current || !identityMatches(captured, token)) return;
      if (!validHistoryPage(page, captured, anchor)) { setHistoryError("Geçmiş yanıtının kimliği, sürümü veya olay sırası doğrulanamadı; mevcut kayıt korundu."); return; }
      const unique = [...changes, ...page.events].filter((event, index, all) => all.findIndex((item) => item.toRevision === event.toRevision) === index);
      const overflow = Math.max(0, unique.length - 32);
      setChanges(unique.slice(-32)); setHistoryDropped((count) => count + overflow);
      setHistoryCursor(page.lastRevision); setHistoryMore(page.hasMore); setHistoryResetAvailable(false);
    } catch (error) {
      if (id === historyId.current && identityMatches(captured, token)) {
        if (error instanceof BridgeError && error.code === "owner_product_history_unavailable") {
          setHistoryError("Bu başlangıç replay penceresinin dışında veya artık kullanılamıyor; görünür kayıt korunuyor.");
          setHistoryResetAvailable(true);
        } else setHistoryError("Geçmiş şu anda yüklenemedi; kayıt ve imleç korundu. Yeniden deneyin.");
      }
    } finally {
      if (id === historyId.current) { historyFlight.current = false; if (mounted.current && token === generation.current) setHistoryBusy(false); }
    }
  };

  const resetHistory = () => {
    const captured = boardRef.current;
    if (!captured || !fresh || !identityMatches(captured, generation.current)) return;
    historyId.current++; historyFlight.current = false;
    setHistoryAnchor(captured.revision); setHistoryCursor(captured.revision); setChanges([]);
    setHistoryDropped(0); setHistoryMore(false); setHistoryError(""); setHistoryResetAvailable(false); setHistoryBusy(false);
  };

  if (!root) return <div className="p-3 text-muted">Önce bir proje açın.</div>;
  if (!board || board.projectRoot !== root) return <div className="grid gap-2 p-3 text-muted"><p>{error || (loading ? "Ortak pano yükleniyor…" : "Sahip oturumu yapılandırılmamış veya bu projeye bağlı değil.")}</p><Button size="sm" onClick={() => void refresh()}>Yenile</Button><Button size="sm" onClick={() => onNavigate("owner")}>Oturum ayarlarını aç</Button></div>;
  const channelMatches = !!owner && owner.projectRoot === root && deliveryRoot === root && deliveryRun === runId && !!deliveryStore && !!deliveryHub && deliveryStore === owner.storePath && deliveryHub === owner.hubPath;
  const candidateMatches = !!candidate && channelMatches && candidate.session_id === board.sessionId && candidate.base_commit === board.baseCommit;
  const contextMatches = fresh && !!candidate && candidate.context_hash === board.contextHash;
  const verified = !!candidate && candidate.verification.status === "pass" && candidate.verification.fingerprint_complete === true && candidate.verification.changed_content === false && contextMatches;
  return <div className="h-full min-w-0 overflow-y-auto p-3 text-text2">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div className="min-w-0"><b>Ortak ürün · sahip görünümü</b><p className="break-all text-faint" style={{ fontSize: "var(--t-caption)" }}>{board.sessionId} · rev {board.revision} · dönem {board.epoch} · {board.state}</p></div><Button size="sm" onClick={() => void refresh()} loading={loading}>Yenile</Button></div>
    <p className="mb-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>Geçici sahip görünümü: görünürken en çok 5 saniyede bir yenilenir; native SSE değildir. Bu pano görev çalıştırmaz, uygulamaz, doğrulamaz veya yayımlamaz.</p>
    <p className="mb-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>Son başarılı pano okuması: {checkedAt ? new Date(checkedAt).toLocaleTimeString() : "yok"} · {fresh ? "güncel" : "güncelliği doğrulanamadı"}</p>
    {!fresh && <p role="alert" className="mb-2 border border-err/40 p-2 text-err">{error || "Pano güncelliği doğrulanamıyor; yazma ve doğrulama devre dışı."}</p>}{error && fresh && <p role="alert" className="mb-2 text-err">{error}</p>}
    {stale && <div className="mb-2 border border-warn/40 p-2 text-warn">Taslak revizyonu eski veya pano erişilemiyor. Otomatik yeniden tabanlanmaz.<Button size="sm" className="mt-2" disabled={!fresh} onClick={() => resetDraft(board)}>Güncel bağlamı yükle / incele</Button></div>}
    <section className="grid min-w-0 gap-2 border-b border-border-w pb-3"><b>Üyeler</b>{board.memberIds.map((member) => <span key={member} className="break-all">{member}{member === board.ownerId ? " · sahip" : ""}</span>)}</section>
    <section className="grid min-w-0 gap-2 border-b border-border-w py-3"><b>Paylaşılan bağlam</b>
      <label className="grid gap-1">Hedef<textarea aria-label="Hedef" className={input} maxLength={4000} value={goal} disabled={writeBusy} onChange={(e) => { setGoal(e.target.value); edit(); }} /></label>
      <label className="grid gap-1">Kararlar · JSON metin dizisi<textarea aria-label="Kararlar · JSON metin dizisi" className={input} value={decisions} disabled={writeBusy} onChange={(e) => { setDecisions(e.target.value); edit(); }} /></label>
      <label className="grid gap-1">Arayüzler · JSON nesnesi (metin değerleri)<textarea aria-label="Arayüzler · JSON nesnesi (metin değerleri)" className={input} value={interfaces} disabled={writeBusy} onChange={(e) => { setInterfaces(e.target.value); edit(); }} /></label>
      <label className="flex min-w-0 gap-2"><input type="checkbox" checked={consent} disabled={!fresh || !dirty || stale || writeBusy || board.state !== "running"} onChange={(e) => setConsent(e.target.checked)} /><span>Bu taslağı sahip olarak, rev {draftRevision.current ?? board.revision} için yayımlamayı onaylıyorum.</span></label><Button size="sm" variant="primary" disabled={!fresh || !consent || !dirty || stale || writeBusy || board.state !== "running"} loading={writeBusy} onClick={() => void writeContext()}>Bağlamı güncelle</Button></section>
     <section className="grid gap-2 py-3"><b>Görev panosu · kaynak durum</b>{board.tasks.map((task) => <article key={task.id} className="min-w-0 border border-border-w p-2"><b className="break-all">{task.id} · {task.owner}</b><p className="break-words">{task.goal}</p><p className="break-words text-faint" style={{ fontSize: "var(--t-caption)" }}>Kaynak durum: {task.status} · kapsam: {task.scopes.join(", ")} · bağlam rev {task.contextRevision}</p><div className="mt-2 flex min-w-0 flex-wrap items-center gap-2"><select className={input + " w-auto max-w-full"} value={taskChoice[task.id] ?? task.status} disabled={writeBusy || !fresh} onChange={(e) => { const status = e.target.value as TaskStatus; setTaskChoice((old) => ({ ...old, [task.id]: status })); setTaskConsent((old) => ({ ...old, [task.id]: undefined as never })); }}><option value="queued">queued</option><option value="running">running</option><option value="waiting">waiting</option><option value="done">done</option></select><label className="flex gap-1 text-faint"><input type="checkbox" checked={taskConsent[task.id]?.revision === board.revision && taskConsent[task.id]?.status === taskChoice[task.id]} disabled={!fresh || writeBusy || board.state !== "running"} onChange={(e) => setTaskConsent((old) => ({ ...old, [task.id]: e.target.checked ? { revision: board.revision, status: taskChoice[task.id] ?? task.status } : undefined as never }))} />revizyon/durum için onay</label><Button size="sm" disabled={!fresh || !taskConsent[task.id] || taskChoice[task.id] === task.status || writeBusy || board.state !== "running"} loading={writeBusy} onClick={() => void updateTask(task)}>Durumu güncelle</Button><Button size="sm" disabled={writeBusy} onClick={() => onNavigate("owner")}>Görev önizlemesi için Oturum</Button></div></article>)}</section>
     <CreateSharedTask board={board} fresh={fresh} busy={writeBusy} onSubmit={createTask} />
    {board.waitingTaskIds.length > 0 && <p className="break-words text-warn">Bekleyen görevler: {board.waitingTaskIds.join(", ")}</p>}{board.overlaps.length > 0 && <section className="grid gap-1 py-2"><b>Kapsam kesişimi önerileri</b><p className="text-faint">Amaçlanan kapsam kesişimleridir; kilit veya gerçek Git çakışması değildir.</p>{board.overlaps.map((o) => <p key={o.tasks.join(":")} className="break-words">{o.tasks.join(" ↔ ")} · {o.shared.join(", ")}</p>)}</section>}
     <section className="grid gap-2 border-t border-border-w py-3"><div className="flex flex-wrap items-center justify-between gap-2"><b>Teklif metadata'sı · açıkça yüklenir</b><Button size="sm" loading={proposalsBusy} disabled={!fresh || proposalsBusy} onClick={() => void loadProposals()}>Teklifleri yükle</Button></div>{proposals && <>{proposals.revision !== board.revision && <p className="text-warn">Liste rev {proposals.revision} gözleminde kaldı; pano revizyonu değişti.</p>}{proposals.proposals.map((p) => <p key={p.proposalId} className="break-all">{p.proposalId} · {p.taskId} · {p.fileCount} dosya {p.staleContext && <b className="text-warn">· eski bağlam</b>}</p>)}</>}</section>
       <ProductHistory anchor={historyAnchor} loaded={historyCursor !== null} events={changes} droppedCount={historyDropped} more={historyMore} busy={historyBusy} fresh={fresh} error={historyError} resetAvailable={historyResetAvailable} onLoad={() => void loadChanges()} onReset={resetHistory} />
    {channelMatches && (deliveryConflicts.length > 0 || candidateMatches) && <section className="grid gap-1 border-t border-border-w py-3"><b>Bu uygulamadaki aday / doğrulama kaydı</b>{deliveryConflicts.map((conflict) => <p key={conflict} className="break-words text-warn">Çakışma: {conflict}</p>)}{candidateMatches && candidate && <><p className="break-all text-faint">Aday rev {candidate.session_revision} · bağlam {candidate.context_hash === board.contextHash ? "eşleşiyor" : "farklı / eski"}</p><p className={verified ? "text-ok" : "text-warn"}>Doğrulama kaydı: {candidate.verification.status}{candidate.verification.status === "pass" && !verified ? " · güvenilir geçiş koşulları veya güncel bağlam eşleşmiyor" : ""}</p><p className="text-faint">Geçmiş doğrulama kaydıdır; güncel diskin kanıtı veya oturum-geneli son kayıt değildir.</p></>}</section>}
    <div className="flex flex-wrap gap-2 border-t border-border-w py-3"><Button size="sm" onClick={() => onNavigate("owner")}>Oturum</Button><Button size="sm" onClick={() => onNavigate("shared")}>Ortak aday</Button></div>
  </div>;
}
