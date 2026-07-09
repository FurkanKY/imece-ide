/* ActivityBar — sol dikey ikon şeridi (VS Code deseni). P1'de görünümler tekli;
   P2/P4'te sohbet-paneli/arama görünümleri eklenir. */

import { Files, Search, GitBranch, Bug, Bot, Settings } from "lucide-react";
import type { SideView as View } from "@/state/ui";

const TOP: { id: View; Icon: typeof Files; label: string }[] = [
  { id: "explorer", Icon: Files, label: "Gezgin" },
  { id: "search", Icon: Search, label: "Ara" },
  { id: "scm", Icon: GitBranch, label: "Kaynak denetimi" },
  { id: "debug", Icon: Bug, label: "Çalıştır ve Debug" },
  { id: "agent", Icon: Bot, label: "AI ekibi" },
];

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
    <nav className="material-panel flex w-[52px] shrink-0 flex-col items-center border-r border-border-w py-2">
      {TOP.map(({ id, Icon, label }) => {
        const on = id === "agent" ? aiPanelVisible : id === active;
        return (
          <button
            key={id}
            title={label}
            aria-label={label}
            aria-pressed={on}
            onClick={() => id === "agent" ? onAgent() : onSelect(id)}
            className="group relative flex h-11 w-full items-center justify-center"
          >
            {on && (
              <span className="absolute left-0 top-1/2 h-5 w-[2px] -translate-y-1/2 rounded-r bg-accent" />
            )}
            <Icon
              size={22}
              strokeWidth={1.8}
              className={
                on ? "text-text" : "text-faint transition-colors group-hover:text-text2"
              }
            />
          </button>
        );
      })}
      <div className="flex-1" />
      <button
        title="Ayarlar"
        aria-label="Ayarlar"
        onClick={onSettings}
        className="flex h-11 w-full items-center justify-center text-faint transition-colors hover:text-text2"
      >
        <Settings size={21} strokeWidth={1.8} />
      </button>
    </nav>
  );
}
