import { ProductChangeEvent } from "@/bridge";
import { Button } from "@/components/ui";

export function ProductHistory({ anchor, loaded, events, droppedCount, more, busy, fresh, error, resetAvailable, onLoad, onReset }: {
  anchor: string | null;
  loaded: boolean;
  events: ProductChangeEvent[];
  droppedCount: number;
  more: boolean;
  busy: boolean;
  fresh: boolean;
  error: string;
  resetAvailable: boolean;
  onLoad: () => void;
  onReset: () => void;
}) {
  return <section className="grid min-w-0 gap-2 border-t border-border-w py-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <b>Anlamlı değişiklikler</b>
      <Button size="sm" loading={busy} disabled={!fresh || busy || !anchor} onClick={onLoad}>Değişiklikleri yükle</Button>
    </div>
    <p className="break-all text-faint" style={{ fontSize: "var(--t-caption)" }}>
      Görünen başlangıç rev {anchor ?? "bekleniyor"}; sayfa imleci yalnızca görüntüleme içindir, onay/CAS veya worker durumu değildir.
    </p>
    {error && <p role="alert" className="text-warn">{error}</p>}
    {resetAvailable && <Button size="sm" disabled={!fresh || busy} onClick={onReset}>Geçmiş başlangıcını güncel panoya taşı</Button>}
    {events.length === 0 && anchor && !error && <p className="text-faint">{loaded ? "Son yüklenen sayfada yeni değişiklik yok." : "Geçmiş henüz yüklenmedi."}</p>}
    {events.map((event) => <article key={event.toRevision} className="grid min-w-0 gap-1 border border-border-w p-2">
      <b className="break-all">rev {event.fromRevision} → {event.toRevision}{event.metadataCheckpoint ? " · metadata checkpoint" : ""}</b>
      {(event.goalChanged || event.decisionsChanged || event.interfaces.added.length || event.interfaces.removed.length || event.interfaces.changed.length) && <p className="break-words">Bağlam değişti{event.goalChanged ? " · hedef" : ""}{event.decisionsChanged ? " · kararlar" : ""}{event.interfaces.added.length ? ` · arayüz anahtarı eklendi: ${event.interfaces.added.join(", ")}` : ""}{event.interfaces.removed.length ? ` · arayüz anahtarı kaldırıldı: ${event.interfaces.removed.join(", ")}` : ""}{event.interfaces.changed.length ? ` · arayüz anahtarı değişti: ${event.interfaces.changed.join(", ")}` : ""}</p>}
      {event.taskChanges.map((task) => <p key={task.taskId} className="break-all">Görev {task.taskId}: {task.change}{task.previousStatus !== task.status ? ` · ${task.previousStatus ?? "∅"} → ${task.status ?? "∅"}` : ""}{task.previousOwner !== task.owner ? ` · atama ${task.previousOwner ?? "∅"} → ${task.owner ?? "∅"}` : ""}{task.fields.length ? ` · ${task.fields.join(", ")}` : ""}</p>)}
      {event.taskChangesTruncated && <p className="text-warn">Görev değişikliklerinin bir bölümü gösterilmiyor ({event.taskChangeCount} toplam).</p>}
      <p className="break-all">İhtiyatlı etki: {event.affectedTaskIds.join(", ") || "yok"} ({event.affectedTaskCount} görev){event.affectedTasksTruncated ? " · liste kısaltıldı" : ""}</p>
      {event.contextChanged && <p className="break-words text-faint">Bağlam değişiminde bu projeksiyon tüm etkin görevleri etkilenmiş sayar; şema 1 arayüz-görev bağımlılıklarını bildirmez. Bu yalnızca ihtiyatlı bir gösterimdir.</p>}
    </article>)}
    {droppedCount > 0 && <p className="text-warn">Yerel gösterim sınırı nedeniyle {droppedCount} eski olay gösterilmiyor; bu, oturum geçmişinin tamamı değildir.</p>}
    {more && <Button size="sm" loading={busy} disabled={!fresh || busy} onClick={onLoad}>Sonraki sayfa</Button>}
  </section>;
}
