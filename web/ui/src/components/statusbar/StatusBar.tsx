/* StatusBar — alt durum çubuğu. P1: proje adı, aktif dosya dili, satır/sütun placeholder.
   P2'de token/maliyet HUD'u buraya bağlanır. */

import { useWorkspace } from "@/state/workspace";
import { useEditor } from "@/state/editor";
import { langForPath } from "@/lib/monaco";

export function StatusBar() {
  const name = useWorkspace((s) => s.name);
  const activeRel = useEditor((s) => s.activeRel);
  const dirty = useEditor((s) => s.tabs.find((t) => t.rel === s.activeRel)?.dirty);

  return (
    <footer
      className="flex h-6 shrink-0 items-center gap-3 border-t border-border-w bg-status px-3 text-muted"
      style={{ fontSize: "var(--t-caption)" }}
    >
      {name && <span className="text-text2">{name}</span>}
      <div className="flex-1" />
      {activeRel && (
        <>
          {dirty && <span className="text-warn">● kaydedilmedi</span>}
          <span>{langForPath(activeRel)}</span>
        </>
      )}
    </footer>
  );
}
