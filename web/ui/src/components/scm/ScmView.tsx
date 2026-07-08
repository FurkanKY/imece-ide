/* ScmView — kenar çubuğu KAYNAK DENETİMİ görünümü (P4, VS Code deseni).
   Dal + ileri/geri, commit kutusu, Hazırlananlar/Değişiklikler grupları;
   satıra tık → merkez Monaco diff; +/−/geri-al satır eylemleri. */

import { useEffect } from "react";
import {
  GitBranch, GitCommitHorizontal, RefreshCw, Plus, Minus, Undo2,
  ArrowUp, ArrowDown, FileQuestion,
} from "lucide-react";
import { ScmChange } from "@/bridge";
import { useScm, SCM_STATUS } from "@/state/scm";
import { useEditor } from "@/state/editor";
import { fileIcon } from "@/lib/fileIcons";

function Row({ change, staged }: { change: ScmChange; staged: boolean }) {
  const { stage, unstage, discard, openDiff } = useScm();
  const activeDiff = useEditor((s) => s.diff?.path ?? null);
  const name = change.path.split("/").pop() ?? change.path;
  const dir = change.path.slice(0, change.path.length - name.length).replace(/\/$/, "");
  const { Icon, color } = fileIcon(name);
  const st = SCM_STATUS[change.status] ?? { color: "var(--muted)", label: change.status };
  const active = activeDiff === change.path;

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => void openDiff(change, staged)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); void openDiff(change, staged); }
      }}
      title={`${change.path} — ${st.label}`}
      className={
        "group flex cursor-pointer items-center gap-1.5 rounded-[var(--r-xs)] py-[3px] pl-2 pr-1 " +
        (active ? "bg-accentdim" : "hover:bg-card")
      }
    >
      <Icon size={14} strokeWidth={1.8} style={{ color }} className="shrink-0" />
      <span className="truncate text-text2" style={{ fontSize: "var(--t-body)" }}>
        {name}
      </span>
      {dir && (
        <span className="min-w-0 truncate text-faint" style={{ fontSize: "var(--t-caption)" }}>
          {dir}
        </span>
      )}
      <span className="flex-1" />
      {/* hover/odak eylemleri */}
      <span className="hidden shrink-0 items-center group-hover:flex group-focus-within:flex">
        {!staged && (
          <button
            onClick={(e) => { e.stopPropagation(); void discard(change); }}
            title="Değişikliği at"
            aria-label="Değişikliği at"
            className="rounded p-0.5 text-faint hover:bg-card2 hover:text-text2"
          >
            <Undo2 size={13} />
          </button>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            void (staged ? unstage([change.path]) : stage([change.path]));
          }}
          title={staged ? "Hazırlıktan çıkar" : "Hazırla (stage)"}
          aria-label={staged ? "Hazırlıktan çıkar" : "Hazırla"}
          className="rounded p-0.5 text-faint hover:bg-card2 hover:text-text2"
        >
          {staged ? <Minus size={13} /> : <Plus size={13} />}
        </button>
      </span>
      <span
        className="w-3 shrink-0 text-center"
        style={{ fontSize: "var(--t-caption)", fontWeight: 700, color: st.color }}
      >
        {change.status}
      </span>
    </div>
  );
}

function Group({ title, changes, staged, action }: {
  title: string;
  changes: ScmChange[];
  staged: boolean;
  action?: { label: string; onClick: () => void; Icon: typeof Plus };
}) {
  if (changes.length === 0) return null;
  return (
    <div className="mb-1">
      <div className="group/g flex h-6 items-center gap-1.5 pl-2 pr-1">
        <span
          className="text-muted"
          style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}
        >
          {title}
        </span>
        <span
          className="rounded-[var(--r-pill)] bg-card px-1.5 text-muted"
          style={{ fontSize: "10px", fontWeight: 700 }}
        >
          {changes.length}
        </span>
        <span className="flex-1" />
        {action && (
          <button
            onClick={action.onClick}
            title={action.label}
            aria-label={action.label}
            className="rounded p-1 text-faint opacity-0 transition-all hover:bg-card hover:text-text2 focus-visible:opacity-100 group-hover/g:opacity-100"
          >
            <action.Icon size={13} strokeWidth={1.9} />
          </button>
        )}
      </div>
      {changes.map((c) => (
        <Row key={(staged ? "s:" : "w:") + c.path} change={c} staged={staged} />
      ))}
    </div>
  );
}

export function ScmView() {
  const s = useScm();
  const canCommit = s.isRepo && s.staged.length > 0 && s.message.trim().length > 0 && !s.busy;

  // görünüm açılınca tazele (fs.changed takibi global — state/scm.ts installScm)
  useEffect(() => {
    void s.refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex h-full flex-col">
      {/* başlık */}
      <div className="group/head flex h-8 shrink-0 items-center pl-3 pr-1.5">
        <span
          className="min-w-0 flex-1 truncate text-muted"
          style={{ fontSize: "var(--t-overline)", fontWeight: "var(--w-overline)", letterSpacing: "var(--ls-overline)" }}
        >
          KAYNAK DENETİMİ
        </span>
        <button
          onClick={() => void s.refresh()}
          title="Yenile"
          aria-label="Yenile"
          className="rounded p-1 text-faint opacity-0 transition-all hover:bg-card hover:text-text2 focus-visible:opacity-100 group-hover/head:opacity-100"
        >
          <RefreshCw size={13} strokeWidth={1.9} />
        </button>
      </div>

      {!s.loaded ? null : !s.isRepo ? (
        <div className="flex flex-col items-center gap-2 px-5 py-8 text-center">
          <FileQuestion size={24} className="text-faint" strokeWidth={1.5} />
          <p className="text-faint" style={{ fontSize: "var(--t-caption)" }}>
            Bu klasör bir git deposu değil. Terminalden <span className="font-mono">git init</span> ile başlatabilirsin.
          </p>
        </div>
      ) : (
        <>
          {/* dal satırı */}
          <div className="mx-2 mb-2 flex h-7 shrink-0 items-center gap-1.5 rounded-[var(--r-sm)] bg-card px-2">
            <GitBranch size={13} className="shrink-0 text-accent" />
            <span className="truncate text-text2" style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}>
              {s.branch}
            </span>
            <span className="flex-1" />
            {s.ahead > 0 && (
              <span className="flex items-center text-muted" style={{ fontSize: "var(--t-caption)" }}>
                {s.ahead}<ArrowUp size={11} />
              </span>
            )}
            {s.behind > 0 && (
              <span className="flex items-center text-muted" style={{ fontSize: "var(--t-caption)" }}>
                {s.behind}<ArrowDown size={11} />
              </span>
            )}
          </div>

          {/* commit kutusu */}
          <div className="mx-2 mb-2 shrink-0">
            <textarea
              value={s.message}
              onChange={(e) => s.setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  void s.commit();
                }
              }}
              placeholder={`"${s.branch}" dalına commit mesajı (Ctrl+Enter)`}
              rows={2}
              className="selectable w-full resize-none rounded-[var(--r-sm)] border border-border-w bg-field px-2.5 py-1.5 text-text outline-none transition-colors placeholder:text-faint focus:border-accent"
              style={{ fontSize: "var(--t-body)" }}
            />
            <button
              onClick={() => void s.commit()}
              disabled={!canCommit}
              className="mt-1 flex w-full items-center justify-center gap-1.5 rounded-[var(--r-sm)] bg-accent px-3 py-1.5 text-on-accent transition-all hover:bg-accent2 disabled:opacity-40"
              style={{ fontSize: "var(--t-label)", fontWeight: "var(--w-label)" }}
            >
              <GitCommitHorizontal size={14} strokeWidth={2} />
              {s.busy ? "Commit ediliyor…" : `Commit (${s.staged.length})`}
            </button>
          </div>

          {/* değişiklik listeleri */}
          <div className="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2">
            <Group
              title="HAZIRLANANLAR"
              changes={s.staged}
              staged
              action={{
                label: "Tümünü hazırlıktan çıkar",
                Icon: Minus,
                onClick: () => void s.unstage(s.staged.map((c) => c.path)),
              }}
            />
            <Group
              title="DEĞİŞİKLİKLER"
              changes={s.unstaged}
              staged={false}
              action={{
                label: "Tümünü hazırla",
                Icon: Plus,
                onClick: () => void s.stage(s.unstaged.map((c) => c.path)),
              }}
            />
            {s.staged.length === 0 && s.unstaged.length === 0 && (
              <p className="px-3 py-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>
                Çalışma ağacı temiz — değişiklik yok.
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}
