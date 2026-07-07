/* session.ts — oturum geri yükleme/kaydetme + pencere kapatma koruması.
   Proje açılınca sekmeleri geri yükler; sekme değişimlerini debounce'la kaydeder;
   window.closeRequested olayında dirty kontrolü yapıp kapatmayı onaylar. */

import { bridge } from "@/bridge";
import { useEditor } from "@/state/editor";
import { confirmDialog } from "@/components/dialogs/dialogs";

export async function restoreSession() {
  try {
    const { openTabs, activeTab } = await bridge.call("session.get", {});
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
    });
  } catch {
    // kritik değil
  }
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
let installed = false;

export function installSessionPersistence() {
  if (installed) return;
  installed = true;

  // sekme listesi/aktif sekme değişince debounce'la kaydet
  useEditor.subscribe((s, prev) => {
    if (s.tabs === prev.tabs && s.activeRel === prev.activeRel) return;
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => void saveSession(), 600);
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
