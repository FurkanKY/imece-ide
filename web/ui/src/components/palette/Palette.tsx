/* Palette — Ctrl+P dosya / Ctrl+K komut paleti (fuzzy overlay).
   ↑/↓ gezin, Enter çalıştır/aç, Esc kapat; eşleşen karakterler vurgulanır. */

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Search, CornerDownLeft } from "lucide-react";
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

  if (!open) return <AnimatePresence />;

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
    <AnimatePresence>
      <motion.div
        key="palette-overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0 }}
        className="fixed inset-0 z-[140] flex items-start justify-center bg-black/40 pt-[12vh]"
        onPointerDown={(e) => {
          if (e.target === e.currentTarget) close();
        }}
      >
        <motion.div
          initial={{ opacity: 0, transform: "translateY(-8px) scale(0.99)" }}
          animate={{ opacity: 1, transform: "translateY(0) scale(1)" }}
          transition={{ duration: 0 }}
          className="material-panel w-[560px] overflow-hidden rounded-[var(--r-lg)] border border-border-w"
          style={{ boxShadow: "var(--bevel-strong), var(--shadow-3)" }}
        >
          <div className="flex items-center gap-2.5 border-b border-border-w px-3.5 py-3">
            <Search size={15} className="shrink-0 text-faint" />
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={onKey}
              placeholder={mode === "files" ? "Dosya adı yaz…" : "Komut yaz…"}
              spellCheck={false}
              className="selectable w-full bg-transparent text-text outline-none placeholder:text-faint"
              style={{ fontSize: "var(--t-body)" }}
            />
            <Kbd className="shrink-0">Esc</Kbd>
          </div>

          <div ref={listRef} className="max-h-[46vh] overflow-y-auto p-1.5">
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
                    onPointerEnter={() => setSel(i)}
                    onClick={() => pick(i)}
                    className={
                      "flex cursor-pointer items-center gap-2.5 rounded-[var(--r-sm)] px-2.5 py-1.5 " +
                      (on ? "bg-accentdim text-text" : "text-text2")
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
                    {r.cmd?.hint && <Kbd className="shrink-0">{r.cmd.hint}</Kbd>}
                    {on && <CornerDownLeft size={13} className="shrink-0 text-faint" />}
                  </div>
                );
              })
            )}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
