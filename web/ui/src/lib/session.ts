/* session.ts — oturum geri yükleme/kaydetme + pencere kapatma koruması.
   Proje açılınca sekmeleri + kabuk düzenini (panel görünürlük/boyut, kenar görünümü)
   geri yükler; değişimleri debounce'la kaydeder; window.closeRequested olayında
   dirty kontrolü yapıp kapatmayı onaylar. */

import { bridge } from "@/bridge";
import { useEditor } from "@/state/editor";
import { useUi, layoutSnapshot, SideView } from "@/state/ui";
import { confirmDialog } from "@/components/dialogs/dialogs";

// Geçerli kenar görünümleri (oturum geri yüklemede beyaz liste). "debug" eklendi
// (eksikti → debug görünümü kalıcı değildi); ölü "agent" pseudo-view kaldırıldı.
const SIDE_VIEWS: SideView[] = ["explorer", "search", "scm", "debug"];

export async function restoreSession() {
  try {
    const { openTabs, activeTab, layout } = await bridge.call("session.get", {});
    // kabuk düzeni (P4) — bilinmeyen sideView değerlerine düşme
    if (layout) {
      useUi.getState().applyLayout({
        ...layout,
        sideView: SIDE_VIEWS.includes(layout.sideView as SideView)
          ? (layout.sideView as SideView)
          : undefined,
      });
    }
    for (const rel of openTabs) {
      try {
        await useEditor.getState().open(rel);
      } catch {
        // dosya silinmiş olabilir — sessiz geç
      }
    }
    if (activeTab && useEditor.getState().tabs.some((t) => t.rel === activeTab)) {
      useEditor.getState().activate(activeTab);
    }
  } catch {
    // oturum yoksa sorun değil
  }
}

async function saveSession() {
  const { tabs, activeRel } = useEditor.getState();
  try {
    await bridge.call("session.save", {
      openTabs: tabs.map((t) => t.rel),
      activeTab: activeRel,
      layout: layoutSnapshot(),
    });
  } catch {
    // kritik değil
  }
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let installed = false;

function scheduleSave() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => void saveSession(), 600);
}

export function installSessionPersistence() {
  if (installed) return;
  installed = true;

  // sekme listesi/aktif sekme değişince debounce'la kaydet
  useEditor.subscribe((s, prev) => {
    if (s.tabs === prev.tabs && s.activeRel === prev.activeRel) return;
    scheduleSave();
  });

  // kabuk düzeni değişince de kaydet (splitter/panel toggle/kenar görünümü)
  useUi.subscribe((s, prev) => {
    if (s.resizing) return; // sürükleme sırasında değil; bırakınca tek kayıt
    if (
      s.sideView === prev.sideView &&
      s.sidebarVisible === prev.sidebarVisible &&
      s.aiPanelVisible === prev.aiPanelVisible &&
      s.bottomVisible === prev.bottomVisible &&
      s.sidebarWidth === prev.sidebarWidth &&
      s.aiPanelWidth === prev.aiPanelWidth &&
      s.bottomHeight === prev.bottomHeight &&
      s.resizing === prev.resizing
    )
      return;
    scheduleSave();
  });

  // dış dosya değişikliği → yüklü klasörleri tazele
  bridge.on("fs.changed", async ({ paths }) => {
    const { useWorkspace } = await import("@/state/workspace");
    const ws = useWorkspace.getState();
    for (const rel of paths) {
      if (ws.children[rel] !== undefined) void ws.loadDir(rel);
    }
  });

  // pencere kapatma koruması (yalnız native'de anlamlı)
  bridge.on("window.closeRequested", async () => {
    const dirty = useEditor.getState().tabs.filter((t) => t.dirty);
    if (dirty.length > 0) {
      const names = dirty.map((t) => t.name).join(", ");
      const ok = await confirmDialog({
        title: "Kaydedilmemiş değişiklikler",
        message:
          dirty.length === 1
            ? `"${names}" kaydedilmedi. Yine de çıkılsın mı?`
            : `${dirty.length} dosya kaydedilmedi (${names}). Yine de çıkılsın mı?`,
        okLabel: "Kaydetmeden Çık",
        danger: true,
      });
      if (!ok) return; // kullanıcı vazgeçti — pencere açık kalır
    }
    await saveSession();
    await bridge.call("window.confirmClose", {});
  });
}
