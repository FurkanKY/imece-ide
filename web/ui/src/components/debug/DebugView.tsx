/* DebugView — kenar çubuğu ÇALIŞTIR VE DEBUG görünümü (P8.2, debugpy/DAP).
   Kontrol şeridi (▶ devam · F10 · F11 · Shift+F11 · ⏹) + çağrı yığını +
   değişkenler (tembel ağaç, variablesReference) + breakpoint listesi. */

import { useCallback, useEffect, useState } from "react";
import {
  ArrowDownToDot, ArrowUpFromDot, Bug, ChevronDown, ChevronRight,
  CircleDot, CornerDownRight, Loader2, Play, Square, Trash2, X,
} from "lucide-react";
import type { DebugFrame, DebugScope, DebugVariable } from "@/bridge/protocol";
import { useDebug } from "@/state/debug";
import { useEditor } from "@/state/editor";
import { fileIcon } from "@/lib/fileIcons";
import { Button } from "@/components/ui";

function Overline({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="flex h-7 shrink-0 items-center px-3 text-muted"
      style={{
        fontSize: "var(--t-overline)",
        fontWeight: "var(--w-overline)",
        letterSpacing: "var(--ls-overline)",
      }}
    >
      {children}
    </div>
  );
}

function CtrlBtn({ label, Icon, onClick, danger = false, disabled = false }: {
  label: string;
  Icon: typeof Play;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      title={label}
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
      className={
        "icon-btn size-7 disabled:opacity-35 " +
        (danger
          ? "text-err hover:text-err"
          : "text-text2")
      }
    >
      <Icon size={15} strokeWidth={2} />
    </button>
  );
}

/** tembel değişken düğümü: ref > 0 → genişletilince çocuklar köprüden çekilir */
function VariableNode({ v, depth }: { v: DebugVariable; depth: number }) {
  const [open, setOpen] = useState(false);
  const [kids, setKids] = useState<DebugVariable[] | null>(null);
  const fetchVariables = useDebug((s) => s.fetchVariables);
  const expandable = v.ref > 0;

  const toggle = useCallback(async () => {
    if (!expandable) return;
    if (!open && kids === null) setKids(await fetchVariables(v.ref));
    setOpen((o) => !o);
  }, [expandable, open, kids, fetchVariables, v.ref]);

  return (
    <div>
      <button
        onClick={() => void toggle()}
        className={
          "flex w-full items-center gap-1 rounded-[var(--r-xs)] py-[2px] pr-2 text-left transition-colors " +
          (expandable ? "hover:bg-card" : "cursor-default")
        }
        style={{ paddingLeft: 8 + depth * 12, fontSize: "var(--t-caption)" }}
      >
        {expandable ? (
          open ? (
            <ChevronDown size={10} className="shrink-0 text-faint" />
          ) : (
            <ChevronRight size={10} className="shrink-0 text-faint" />
          )
        ) : (
          <span className="w-[10px] shrink-0" />
        )}
        <span className="shrink-0 text-text2" style={{ fontFamily: "var(--font-mono)" }}>
          {v.name}
        </span>
        <span className="shrink-0 text-faint">=</span>
        <span
          className="min-w-0 truncate text-muted"
          style={{ fontFamily: "var(--font-mono)" }}
          title={v.value}
        >
          {v.value}
        </span>
      </button>
      {open && kids?.map((k, i) => <VariableNode key={k.name + i} v={k} depth={depth + 1} />)}
    </div>
  );
}

function Variables({ frameId }: { frameId: number }) {
  const fetchScopes = useDebug((s) => s.fetchScopes);
  const fetchVariables = useDebug((s) => s.fetchVariables);
  const [scopes, setScopes] = useState<(DebugScope & { vars: DebugVariable[] })[] | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      const sc = await fetchScopes(frameId);
      // pahalı olmayan kapsamların değişkenleri hemen (Locals önce gelir)
      const filled = await Promise.all(
        sc.map(async (s) => ({
          ...s,
          vars: s.expensive ? [] : await fetchVariables(s.ref),
        })),
      );
      if (alive) setScopes(filled);
    })();
    return () => {
      alive = false;
    };
  }, [frameId, fetchScopes, fetchVariables]);

  if (scopes === null) {
    return (
      <div className="flex items-center gap-2 px-3 py-1.5 text-faint" style={{ fontSize: "var(--t-caption)" }}>
        <Loader2 size={11} className="animate-spin" /> yükleniyor…
      </div>
    );
  }
  return (
    <div className="px-1.5">
      {scopes.map((s) => (
        <div key={s.ref} className="mb-1">
          <div className="px-1.5 py-0.5 text-faint" style={{ fontSize: "var(--t-caption)", fontWeight: "var(--w-label)" }}>
            {s.name}
          </div>
          {s.vars.map((v, i) => (
            <VariableNode key={v.name + i} v={v} depth={0} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function DebugView() {
  const dbg = useDebug();
  const openAt = useEditor((s) => s.openAt);
  const activeRel = useEditor((s) => s.activeRel);
  const [frameId, setFrameId] = useState<number | null>(null);

  // durunca üst kare seçili gelsin
  useEffect(() => {
    setFrameId(dbg.frames.length ? dbg.frames[0].id : null);
  }, [dbg.frames]);

  const bpEntries = Object.entries(dbg.breakpoints).flatMap(([path, lines]) =>
    lines.map((line) => ({ path, line })),
  );
  const stopped = dbg.status === "stopped";
  const active = dbg.status === "running" || stopped || dbg.status === "starting";

  return (
    <div className="flex h-full flex-col">
      <Overline>ÇALIŞTIR VE DEBUG</Overline>

      {/* kontrol şeridi / başlat */}
      {active ? (
        <div className="material-card mx-2.5 mb-2 flex items-center gap-0.5 rounded-[var(--r-sm)] border border-border-w px-1 py-0.5">
          <CtrlBtn label="Devam (F5)" Icon={Play} onClick={dbg.cont} disabled={!stopped} />
          <CtrlBtn label="Üzerinden Adımla (F10)" Icon={CornerDownRight} onClick={dbg.next} disabled={!stopped} />
          <CtrlBtn label="İçine Gir (F11)" Icon={ArrowDownToDot} onClick={dbg.stepIn} disabled={!stopped} />
          <CtrlBtn label="Dışına Çık (Shift+F11)" Icon={ArrowUpFromDot} onClick={dbg.stepOut} disabled={!stopped} />
          <div className="flex-1" />
          <CtrlBtn label="Durdur (Shift+F5)" Icon={Square} onClick={() => void dbg.stop()} danger />
        </div>
      ) : (
        <div className="px-2.5 pb-1">
          <Button
            onClick={() => void dbg.start(activeRel)}
            variant="primary"
            size="sm"
            block
            icon={Bug}
          >
            Debug Başlat (F5)
          </Button>
          <p className="mt-1.5 px-1 text-faint" style={{ fontSize: "var(--t-caption)" }}>
            Aktif .py dosyası breakpoint'lerle koşar. Satır numarasının soluna
            tıklayarak (veya F9) breakpoint koy.
          </p>
        </div>
      )}

      {/* durum satırı */}
      {active && (
        <div className="flex items-center gap-1.5 px-3.5 pb-1.5" style={{ fontSize: "var(--t-caption)" }}>
          {stopped ? (
            <>
              <span className="size-1.5 rounded-full bg-warn" />
              <span className="text-warn">
                durdu · {dbg.stoppedAt?.path}:{dbg.stoppedAt?.line} ({dbg.stoppedAt?.reason})
              </span>
            </>
          ) : (
            <>
              <Loader2 size={11} className="animate-spin text-accent" />
              <span className="text-muted">
                {dbg.status === "starting" ? "başlatılıyor…" : "çalışıyor…"} {dbg.file}
              </span>
            </>
          )}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto pb-2">
        {/* çağrı yığını */}
        {stopped && (
          <>
            <Overline>ÇAĞRI YIĞINI</Overline>
            <div className="px-1.5">
              {dbg.frames.map((f: DebugFrame) => (
                <button
                  key={f.id}
                  onClick={() => {
                    setFrameId(f.id);
                    if (f.path) void openAt(f.path, f.line, 1);
                  }}
                  className={
                    "flex w-full items-baseline gap-2 rounded-[var(--r-xs)] px-2 py-[2.5px] text-left transition-colors hover:bg-card " +
                    (frameId === f.id ? "bg-card" : "")
                  }
                  style={{ fontSize: "var(--t-caption)" }}
                >
                  <span className="min-w-0 truncate text-text2" style={{ fontFamily: "var(--font-mono)" }}>
                    {f.name}
                  </span>
                  <span className="ml-auto shrink-0 text-faint" style={{ fontFamily: "var(--font-mono)" }}>
                    {f.path.split("/").pop()}:{f.line}
                  </span>
                </button>
              ))}
            </div>

            <Overline>DEĞİŞKENLER</Overline>
            {frameId !== null && <Variables key={frameId} frameId={frameId} />}
          </>
        )}

        {/* breakpoint listesi */}
        <div className="flex h-7 shrink-0 items-center px-3 text-muted">
          <span
            style={{
              fontSize: "var(--t-overline)",
              fontWeight: "var(--w-overline)",
              letterSpacing: "var(--ls-overline)",
            }}
          >
            BREAKPOINT'LER
          </span>
          {bpEntries.length > 0 && (
            <button
              onClick={dbg.clearBreakpoints}
              title="Tümünü kaldır"
              className="icon-btn ml-auto size-6 hover:text-err"
            >
              <Trash2 size={12} />
            </button>
          )}
        </div>
        <div className="px-1.5">
          {bpEntries.length === 0 && (
            <p className="px-2 text-faint" style={{ fontSize: "var(--t-caption)" }}>
              Breakpoint yok.
            </p>
          )}
          {bpEntries.map(({ path, line }) => {
            const name = path.split("/").pop() ?? path;
            const { Icon, color } = fileIcon(name);
            return (
              <div
                key={path + line}
                className="group flex w-full items-center gap-1.5 rounded-[var(--r-xs)] px-2 py-[2.5px] transition-colors hover:bg-card"
                style={{ fontSize: "var(--t-caption)" }}
              >
                <CircleDot size={10} className="shrink-0 text-err" />
                <button
                  onClick={() => void openAt(path, line, 1)}
                  className="flex min-w-0 flex-1 items-baseline gap-1.5 text-left"
                >
                  <Icon size={12} strokeWidth={1.8} style={{ color }} className="shrink-0 self-center" />
                  <span className="min-w-0 truncate text-text2">{path}</span>
                  <span className="shrink-0 text-faint" style={{ fontFamily: "var(--font-mono)" }}>
                    {line}
                  </span>
                </button>
                <button
                  onClick={() => dbg.removeBreakpoint(path, line)}
                  title="Kaldır"
                  className="icon-btn size-5 opacity-0 transition-opacity hover:text-err focus-visible:opacity-100 group-hover:opacity-100"
                >
                  <X size={11} />
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
