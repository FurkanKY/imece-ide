/* Palette — Ctrl+P dosya / Ctrl+K komut paleti (fuzzy overlay).
   ↑/↓ gezin, Enter çalıştır/aç, Esc kapat; eşleşen karakterler vurgulanır. */

import { useEffect, useMemo, useRef, useState } from "react";
import { Command as CommandIcon, FileSearch, CornerDownLeft } from "lucide-react";
import { usePalette, Command } from "./paletteStore";
import { fuzzyFilter, FuzzyHit } from "@/lib/fuzzy";
import { fileIcon } from "@/lib/fileIcons";
import { useEditor } from "@/state/editor";
import { EmptyState, Kbd } from "@/components/ui";

function Highlight({ text, hit }: { text: string; hit: FuzzyHit }) {
  const marks = new Set(hit.indices);
  return (
    <>
      {[...text].map((ch, i) =>
        marks.has(i) ? (
          <span key={i} className="text-accent" style={{ fontWeight: 700 }}>
            {ch}
          </span>
        ) : (
          <span key={i}>{ch}</span>
        ),
      )}
    </>
  );
}

export function Palette() {
  const { open, mode, files, commands, close } = usePalette();
  const openFile = useEditor((s) => s.open);
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSel(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open, mode]);

  const results = useMemo(() => {
    if (mode === "files") {
      return fuzzyFilter(query, files, (f) => f).map(({ item, hit }) => ({
        key: item,
        hit,
        file: item as string,
        cmd: null as Command | null,
      }));
    }
    return fuzzyFilter(query, commands, (c) => c.label).map(({ item, hit }) => ({
      key: item.id,
      hit,
      file: null as string | null,
      cmd: item,
    }));
  }, [query, mode, files, commands]);

  useEffect(() => setSel(0), [results.length, query]);

  // seçili öğe görünür kalsın
  useEffect(() => {
    listRef.current
      ?.querySelector(`[data-idx="${sel}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  if (!open) return null;

  const pick = (i: number) => {
    const r = results[i];
    if (!r) return;
    close();
    if (r.file) void openFile(r.file);
    else if (r.cmd) void r.cmd.run();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, results.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); pick(sel); }
  };

  return (
    <div
        className="fixed inset-0 z-[var(--z-overlay)] flex items-start justify-center overflow-y-auto bg-black/55 p-4 pt-[12vh]"
        onPointerDown={(e) => {
          if (e.target === e.currentTarget) close();
        }}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-label={mode === "files" ? "Dosyaya git" : "Komut Merkezi"}
          className="w-[min(600px,calc(100vw-2rem))] overflow-hidden rounded-[var(--r-md)] border border-border-w bg-panel"
          style={{ boxShadow: "var(--shadow-2)" }}
        >
          <div className="flex items-center gap-2.5 border-b border-border-w px-4 py-3">
            {mode === "files" ? <FileSearch size={15} className="shrink-0 text-muted" /> : <CommandIcon size={15} className="shrink-0 text-muted" />}
            <div className="min-w-0 flex-1">
              <p className="text-muted" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
                {mode === "files" ? "Dosyaya git" : "Komut Merkezi"}
              </p>
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={onKey}
                placeholder={mode === "files" ? "Dosya adı" : "Komut ara"}
                aria-label={mode === "files" ? "Dosya ara" : "Komut ara"}
                spellCheck={false}
                className="selectable w-full bg-transparent text-text outline-none placeholder:text-faint"
                style={{ fontSize: "var(--t-body)" }}
              />
            </div>
            <Kbd className="shrink-0">Esc</Kbd>
          </div>

          <div ref={listRef} role="listbox" aria-label={mode === "files" ? "Dosya sonuçları" : "Komut sonuçları"} className="max-h-[46vh] overflow-y-auto py-1.5">
            {results.length === 0 ? (
              <EmptyState title="Eşleşme yok" description="Farklı bir dosya adı veya komut deneyin." className="py-7" />
            ) : (
              results.map((r, i) => {
                const on = i === sel;
                const label = r.file ?? r.cmd!.label;
                const FIcon = r.file ? fileIcon(r.file.split("/").pop()!).Icon : r.cmd!.Icon;
                const iconColor = r.file ? fileIcon(r.file.split("/").pop()!).color : undefined;
                return (
                  <div
                    key={r.key}
                    data-idx={i}
                    role="option"
                    aria-selected={on}
                    onPointerEnter={() => setSel(i)}
                    onClick={() => pick(i)}
                    className={
                      "flex cursor-pointer items-center gap-2.5 border-l-2 px-4 py-2 " +
                      (on ? "border-accent bg-accentdim/60 text-text" : "border-transparent text-text2 hover:bg-card/45")
                    }
                    style={{ fontSize: "var(--t-label)" }}
                  >
                    {FIcon && (
                      <FIcon size={14} strokeWidth={1.9} className="shrink-0 opacity-90"
                             style={iconColor ? { color: iconColor } : undefined} />
                    )}
                    <span className="min-w-0 flex-1 truncate">
                      <Highlight text={label} hit={r.hit} />
                    </span>
                    {r.cmd?.id.startsWith("ai-") && <span className="shrink-0 text-faint" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-caption)" }}>AI</span>}
                    {r.cmd?.hint && <Kbd className="shrink-0">{r.cmd.hint}</Kbd>}
                    {on && <CornerDownLeft size={13} className="shrink-0 text-faint" />}
                  </div>
                );
              })
            )}
          </div>
          <div className="flex items-center justify-between border-t border-border-w px-3.5 py-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            <span>{results.length} {mode === "files" ? "dosya" : "eylem"}</span>
            <span className="flex items-center gap-1.5"><Kbd>↑ ↓</Kbd> seç <Kbd>Enter</Kbd> çalıştır</span>
          </div>
        </div>
      </div>
  );
}
