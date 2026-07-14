/* SearchView — kenar çubuğu global arama (Ctrl+Shift+F).
   Sorgu + (Aa / .*) toggle'ları → dosyaya gruplu sonuçlar → tık: satıra git. */

import { useEffect, useMemo, useRef } from "react";
import { CaseSensitive, Regex, Search, X, Loader2, ChevronRight, SearchX } from "lucide-react";
import { useSearch } from "@/state/search";
import { useEditor } from "@/state/editor";
import { fileIcon } from "@/lib/fileIcons";
import { SearchMatch } from "@/bridge";
import { EmptyState, IconButton } from "@/components/ui";

function OptionToggle({ on, label, Icon, onClick }: {
  on: boolean;
  label: string;
  Icon: typeof Regex;
  onClick: () => void;
}) {
  return (
    <button
      title={label}
      aria-label={label}
      aria-pressed={on}
      onClick={onClick}
      className={
        "icon-btn size-6 " +
        (on ? "bg-accentdim text-accent hover:bg-accentdim hover:text-accent" : "")
      }
    >
      <Icon size={13} strokeWidth={2} />
    </button>
  );
}

export function SearchView() {
  const s = useSearch();
  const openAt = useEditor((st) => st.openAt);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    s.install();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Ctrl+Shift+F → odak isteği
  useEffect(() => {
    if (s.focusNonce > 0) {
      requestAnimationFrame(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      });
    }
  }, [s.focusNonce]);

  const groups = useMemo(() => {
    const by = new Map<string, SearchMatch[]>();
    for (const m of s.matches) {
      const arr = by.get(m.path);
      if (arr) arr.push(m);
      else by.set(m.path, [m]);
    }
    return [...by.entries()];
  }, [s.matches]);

  return (
    <div className="flex h-full flex-col">
      <div
        className="material-panel flex h-8 shrink-0 items-center border-b border-border-w px-3 text-muted"
        style={{
          fontSize: "var(--t-overline)",
          fontWeight: "var(--w-overline)",
          letterSpacing: "var(--ls-overline)",
        }}
      >
        ARA
      </div>

      {/* sorgu satırı */}
      <div className="px-2.5 pb-2">
        <div className="material-card flex items-center gap-1 rounded-[var(--r-sm)] border border-border-w px-2 py-1 transition-colors focus-within:border-accent">
          <Search size={12.5} className="shrink-0 text-faint" />
          <input
            ref={inputRef}
            value={s.query}
            onChange={(e) => s.setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void s.start();
              if (e.key === "Escape" && s.searching) void s.cancel();
            }}
            placeholder="Projede ara… (Enter)"
            spellCheck={false}
            className="selectable w-full min-w-0 bg-transparent text-text outline-none placeholder:text-faint"
            style={{ fontSize: "var(--t-label)" }}
          />
          {s.searching ? (
            <IconButton size="sm" icon={X} label="Aramayı durdur" title="Durdur" className="hover:text-err" onClick={() => void s.cancel()} />
          ) : (
            <>
              <OptionToggle on={s.caseSensitive} label="Büyük/küçük harfe duyarlı" Icon={CaseSensitive} onClick={s.toggleCase} />
              <OptionToggle on={s.regex} label="Düzenli ifade" Icon={Regex} onClick={s.toggleRegex} />
            </>
          )}
        </div>
        <div className="mt-1 flex items-center gap-2 px-1 text-faint" style={{ fontSize: "var(--t-caption)" }}>
          {s.searching && <Loader2 size={11} className="animate-spin" />}
          {s.searching
            ? "aranıyor…"
            : s.total > 0
              ? `${s.total} eşleşme · ${groups.length} dosya${s.limitHit ? " (sınıra takıldı)" : ""}`
              : s.query && s.matches.length === 0 && s.total === 0
                ? "" : ""}
        </div>
      </div>

      {/* sonuçlar */}
      <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2">
        {groups.map(([path, matches]) => {
          const name = path.split("/").pop() ?? path;
          const { Icon, color } = fileIcon(name);
          return (
            <div key={path} className="mb-1">
              <div className="flex items-center gap-1.5 px-1.5 py-1 text-text2" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
                <ChevronRight size={11} className="rotate-90 text-faint" />
                <Icon size={13} strokeWidth={1.8} style={{ color }} className="shrink-0" />
                <span className="min-w-0 truncate">{name}</span>
                <span className="truncate text-faint" style={{ fontWeight: 400 }}>{path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ""}</span>
                <span className="ml-auto shrink-0 rounded-[var(--r-pill)] bg-card px-1.5 text-faint" style={{ fontSize: "10px" }}>
                  {matches.length}
                </span>
              </div>
              {matches.map((m, i) => (
                <button
                  key={i}
                  onClick={() => void openAt(m.path, m.line, m.col)}
                  className="flex w-full items-baseline gap-2 rounded-[var(--r-xs)] py-[2.5px] pl-6 pr-2 text-left transition-colors hover:bg-card"
                >
                  <span className="shrink-0 text-faint" style={{ fontSize: "var(--t-caption)", fontFamily: "var(--font-mono)" }}>
                    {m.line}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-muted" style={{ fontSize: "var(--t-caption)", fontFamily: "var(--font-mono)" }}>
                    {m.preview}
                  </span>
                </button>
              ))}
            </div>
          );
        })}
        {s.error ? (
          <EmptyState
            icon={SearchX}
            title="Arama tamamlanamadı"
            description={s.error}
            action={<button onClick={() => void s.start()} className="text-accent" style={{ fontSize: "var(--t-label)" }}>Tekrar Dene</button>}
          />
        ) : !s.searching && s.query.trim().length >= 2 && s.matches.length === 0 && s.searchId ? (
          <EmptyState icon={SearchX} title="Eşleşme bulunamadı" description="Sorguyu veya arama seçeneklerini değiştirip yeniden dene." />
        ) : null}
      </div>
    </div>
  );
}
