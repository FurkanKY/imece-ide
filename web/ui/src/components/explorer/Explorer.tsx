/* Explorer — lazy dosya ağacı. Girinti kılavuzları, başlıkta hover eylemleri,
   aktif dosyada sol accent şeridi. Klasör tıkla → genişle; dosya tıkla → aç. */

import { useState } from "react";
import { ChevronRight, ChevronsDownUp, FilePlus2, FolderPlus, Folder, FolderOpen, Loader2, RefreshCw } from "lucide-react";
import { DirEntry } from "@/bridge";
import { useWorkspace } from "@/state/workspace";
import { useEditor } from "@/state/editor";
import { useScmDecorations, SCM_STATUS } from "@/state/scm";
import { fileIcon } from "@/lib/fileIcons";
import { ExplorerMenu } from "./ExplorerMenu";

const INDENT = 12;

/** git dekorasyonları (P6): dosya → durum harfi, değişen klasör kümesi */
type Deco = { files: Map<string, string>; dirs: Set<string> };

/* sürükle-taşı (P6.4): sürüklenen girdinin rel'i — dragover'da dataTransfer
   okunamadığı için modül değişkeni */
let dragSrc: string | null = null;

function Row({ entry, depth, deco }: { entry: DirEntry; depth: number; deco: Deco }) {
  const { expanded, children, loading, toggleDir, moveEntry } = useWorkspace();
  const openFile = useEditor((s) => s.open);
  const activeRel = useEditor((s) => s.activeRel);
  const [dropOver, setDropOver] = useState(false);

  const isOpen = expanded.has(entry.rel);
  const isActive = !entry.isDir && activeRel === entry.rel;
  const isLoading = loading.has(entry.rel);
  const scmStatus = entry.isDir ? null : deco.files.get(entry.rel) ?? null;
  const scmColor = scmStatus ? SCM_STATUS[scmStatus]?.color : null;
  const dirChanged = entry.isDir && deco.dirs.has(entry.rel);

  const onClick = () => {
    if (entry.isDir) void toggleDir(entry.rel);
    else void openFile(entry.rel);
  };

  const { Icon, color } = entry.isDir
    ? { Icon: isOpen ? FolderOpen : Folder, color: "#7a8aa0" }
    : fileIcon(entry.name);

  return (
    <>
      <ExplorerMenu entry={entry}>
      <button
        onClick={onClick}
        title={entry.name}
        draggable
        onDragStart={(e) => {
          dragSrc = entry.rel;
          e.dataTransfer.effectAllowed = "move";
        }}
        onDragEnd={() => { dragSrc = null; }}
        onDragOver={(e) => {
          // yalnız klasörler hedef; kendine/kendi altına bırakılamaz
          if (entry.isDir && dragSrc && dragSrc !== entry.rel && !entry.rel.startsWith(dragSrc + "/")) {
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            setDropOver(true);
          }
        }}
        onDragLeave={() => setDropOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setDropOver(false);
          if (entry.isDir && dragSrc) void moveEntry(dragSrc, entry.rel);
          dragSrc = null;
        }}
        className={
          "group relative flex w-full items-center gap-1.5 rounded-[var(--r-xs)] py-[3px] pr-2 text-left transition-colors duration-100 " +
          (isActive ? "bg-accentdim text-text" : "text-text2 hover:bg-card") +
          (dropOver ? " outline outline-1 outline-accent" : "")
        }
        style={{ paddingLeft: depth * INDENT + 6 }}
      >
        {/* girinti kılavuzları */}
        {Array.from({ length: depth }).map((_, i) => (
          <span
            key={i}
            className="pointer-events-none absolute bottom-0 top-0 w-px bg-line"
            style={{ left: i * INDENT + 12 }}
          />
        ))}
        {/* aktif dosya accent şeridi */}
        {isActive && (
          <span className="absolute bottom-1 left-0 top-1 w-[2px] rounded-r bg-accent" />
        )}
        <span className="flex size-4 shrink-0 items-center justify-center">
          {entry.isDir ? (
            isLoading ? (
              <Loader2 size={12} className="animate-spin text-faint" />
            ) : (
              <ChevronRight
                size={13}
                className={
                  "text-faint transition-transform duration-[var(--dur-fast)] " +
                  (isOpen ? "rotate-90" : "")
                }
              />
            )
          ) : null}
        </span>
        <Icon size={15} strokeWidth={1.8} style={{ color }} className="shrink-0" />
        <span
          className="truncate"
          style={{ fontSize: "var(--t-body)", color: scmColor ?? undefined }}
        >
          {entry.name}
        </span>
        {dirChanged && (
          <span
            className="ml-auto mr-1 size-[5px] shrink-0 rounded-full"
            style={{ background: "var(--amber)", opacity: 0.8 }}
            title="Bu klasörde git değişikliği var"
          />
        )}
        {scmStatus && (
          <span
            className="ml-auto shrink-0 pr-0.5"
            style={{ fontSize: "var(--t-caption)", fontWeight: 700, color: scmColor ?? undefined }}
            title={SCM_STATUS[scmStatus]?.label ?? scmStatus}
          >
            {scmStatus}
          </span>
        )}
      </button>
      </ExplorerMenu>
      {entry.isDir && isOpen &&
        (children[entry.rel] ?? []).map((child) => (
          <Row key={child.rel} entry={child} depth={depth + 1} deco={deco} />
        ))}
    </>
  );
}

function HeaderAction({ Icon, label, onClick }: {
  Icon: typeof FilePlus2;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      title={label}
      aria-label={label}
      onClick={onClick}
      className="rounded p-1 text-faint opacity-0 transition-all hover:bg-card hover:text-text2 focus-visible:opacity-100 group-hover/head:opacity-100"
    >
      <Icon size={13} strokeWidth={1.9} />
    </button>
  );
}

export function Explorer() {
  const { name, children, newFile, newFolder, loadDir } = useWorkspace();
  const roots = children[""] ?? [];
  const deco = useScmDecorations();

  const collapseAll = () =>
    useWorkspace.setState({ expanded: new Set() });

  return (
    <div className="flex h-full flex-col">
      <div className="group/head flex h-8 shrink-0 items-center pl-3 pr-1.5">
        <span
          className="min-w-0 flex-1 truncate text-muted"
          style={{
            fontSize: "var(--t-overline)",
            fontWeight: "var(--w-overline)",
            letterSpacing: "var(--ls-overline)",
          }}
        >
          {(name ?? "GEZGİN").toLocaleUpperCase("tr")}
        </span>
        <HeaderAction Icon={FilePlus2} label="Yeni Dosya" onClick={() => void newFile("")} />
        <HeaderAction Icon={FolderPlus} label="Yeni Klasör" onClick={() => void newFolder("")} />
        <HeaderAction Icon={RefreshCw} label="Yenile" onClick={() => void loadDir("")} />
        <HeaderAction Icon={ChevronsDownUp} label="Tümünü Daralt" onClick={collapseAll} />
      </div>
      <ExplorerMenu entry={null}>
        <div
          className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2"
          onDragOver={(e) => {
            // boş alana bırak → köke taşı (satır hedefleri stopPropagation yapar)
            if (dragSrc && dragSrc.includes("/")) e.preventDefault();
          }}
          onDrop={(e) => {
            e.preventDefault();
            if (dragSrc && dragSrc.includes("/")) {
              void useWorkspace.getState().moveEntry(dragSrc, "");
            }
            dragSrc = null;
          }}
        >
          {roots.length === 0 ? (
            <p className="px-3 py-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>
              Boş klasör. Sağ-tık → Yeni Dosya.
            </p>
          ) : (
            roots.map((e) => <Row key={e.rel} entry={e} depth={0} deco={deco} />)
          )}
        </div>
      </ExplorerMenu>
    </div>
  );
}
