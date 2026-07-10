/* ActivityBar — sol dikey ikon şeridi (VS Code deseni). Görünüm anahtarları
   (explorer/search/scm/debug) + ayrı AI paneli toggle'ı + ayarlar.
   Aktif gösterge: sol accent çubuğu (şekil) + metin rengi — yalnız renge güvenmez.
   Tooltip'ler ad + (varsa) gerçek kısayolu Kbd ile gösterir. */

import { Files, Search, GitBranch, Bug, Bot, Settings, type LucideIcon } from "lucide-react";
import type { SideView as View } from "@/state/ui";
import { Tooltip, Kbd } from "@/components/ui";

interface BarItem {
  Icon: LucideIcon;
  label: string;
  shortcut?: string; // yalnız keymap.ts'te GERÇEKTEN bağlı olanlar
}

const VIEWS: (BarItem & { id: View })[] = [
  { id: "explorer", Icon: Files, label: "Gezgin" },
  { id: "search", Icon: Search, label: "Ara", shortcut: "Ctrl ⇧ F" },
  { id: "scm", Icon: GitBranch, label: "Kaynak denetimi", shortcut: "Ctrl ⇧ G" },
  { id: "debug", Icon: Bug, label: "Çalıştır ve Debug" },
];

function BarButton({
  Icon,
  label,
  shortcut,
  active,
  onClick,
}: BarItem & { active: boolean; onClick: () => void }) {
  return (
    <Tooltip
      side="right"
      content={
        <>
          {label}
          {shortcut && <Kbd>{shortcut}</Kbd>}
        </>
      }
    >
      <button
        aria-label={label}
        aria-pressed={active}
        onClick={onClick}
        className="group relative flex h-11 w-full items-center justify-center outline-none"
      >
        {active && (
          <span className="absolute left-0 top-1/2 h-5 w-[2px] -translate-y-1/2 rounded-r bg-accent" />
        )}
        <Icon
          size={20}
          strokeWidth={1.8}
          className={active ? "text-text" : "text-faint transition-colors group-hover:text-text2"}
        />
      </button>
    </Tooltip>
  );
}

export function ActivityBar({
  active,
  onSelect,
  aiPanelVisible,
  onAgent,
  onSettings,
}: {
  active: View;
  onSelect: (v: View) => void;
  aiPanelVisible: boolean;
  onAgent: () => void;
  onSettings: () => void;
}) {
  return (
    <nav className="material-panel flex w-12 shrink-0 flex-col items-center border-r border-border-w py-2">
      {VIEWS.map((v) => (
        <BarButton key={v.id} {...v} active={v.id === active} onClick={() => onSelect(v.id)} />
      ))}

      <div className="flex-1" />

      {/* AI paneli toggle'ı — bir kenar görünümü DEĞİL; sağ paneli açar/kapatır */}
      <BarButton
        Icon={Bot}
        label="AI ekibi"
        shortcut="Ctrl J"
        active={aiPanelVisible}
        onClick={onAgent}
      />
      <BarButton Icon={Settings} label="Ayarlar" active={false} onClick={onSettings} />
    </nav>
  );
}
