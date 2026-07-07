/* Explorer — lazy dosya ağacı. Klasör tıkla → genişle (fs.listDir); dosya tıkla →
   editörde aç. Token'lı hover/seçili durumları; boş/yükleniyor durumları. */

import { ChevronRight, Folder, FolderOpen, Loader2 } from "lucide-react";
import { DirEntry } from "@/bridge";
import { useWorkspace } from "@/state/workspace";
import { useEditor } from "@/state/editor";
import { fileIcon } from "@/lib/fileIcons";

const INDENT = 12;

function Row({ entry, depth }: { entry: DirEntry; depth: number }) {
  const { expanded, children, loading, toggleDir } = useWorkspace();
  const openFile = useEditor((s) => s.open);
  const activeRel = useEditor((s) => s.activeRel);

  const isOpen = expanded.has(entry.rel);
  const isActive = !entry.isDir && activeRel === entry.rel;
  const isLoading = loading.has(entry.rel);

  const onClick = () => {
    if (entry.isDir) void toggleDir(entry.rel);
    else void openFile(entry.rel);
  };

  const { Icon, color } = entry.isDir
    ? { Icon: isOpen ? FolderOpen : Folder, color: "#7a8aa0" }
    : fileIcon(entry.name);

  return (
    <>
      <button
        onClick={onClick}
        title={entry.name}
        className={
          "flex w-full items-center gap-1.5 rounded-[var(--r-xs)] py-[3px] pr-2 text-left transition-colors duration-100 " +
          (isActive
            ? "bg-accentdim text-text"
            : "text-text2 hover:bg-card")
        }
        style={{ paddingLeft: depth * INDENT + 6 }}
      >
        <span className="flex size-4 shrink-0 items-center justify-center">
          {entry.isDir ? (
            isLoading ? (
              <Loader2 size={12} className="animate-spin text-faint" />
            ) : (
              <ChevronRight
                size={13}
                className={"text-faint transition-transform " + (isOpen ? "rotate-90" : "")}
              />
            )
          ) : null}
        </span>
        <Icon size={15} strokeWidth={1.8} style={{ color }} className="shrink-0" />
        <span className="truncate" style={{ fontSize: "var(--t-body)" }}>
          {entry.name}
        </span>
      </button>
      {entry.isDir && isOpen &&
        (children[entry.rel] ?? []).map((child) => (
          <Row key={child.rel} entry={child} depth={depth + 1} />
        ))}
    </>
  );
}

export function Explorer() {
  const { name, children } = useWorkspace();
  const roots = children[""] ?? [];

  return (
    <div className="flex h-full flex-col">
      <div
        className="flex h-8 items-center px-3 text-muted"
        style={{
          fontSize: "var(--t-overline)",
          fontWeight: "var(--w-overline)",
          letterSpacing: "var(--ls-overline)",
        }}
      >
        {(name ?? "GEZGİN").toLocaleUpperCase("tr")}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2">
        {roots.length === 0 ? (
          <p className="px-3 py-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            Boş klasör.
          </p>
        ) : (
          roots.map((e) => <Row key={e.rel} entry={e} depth={0} />)
        )}
      </div>
    </div>
  );
}
