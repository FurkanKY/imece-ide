/* TabMenu — editör sekmesi sağ-tık menüsü (P6.3, Radix ContextMenu).
   Toplu kapatmalarda kaydedilmemiş sekmeler açık kalır (veri kaybı yok). */

import { ContextMenu } from "radix-ui";
import {
  X, XCircle, ArrowRightToLine, ClipboardCopy, ExternalLink,
  type LucideIcon,
} from "lucide-react";
import { useEditor } from "@/state/editor";
import { useWorkspace } from "@/state/workspace";

function Item(props: { Icon: LucideIcon; label: string; onSelect: () => void }) {
  return (
    <ContextMenu.Item
      onSelect={props.onSelect}
      className="flex cursor-default select-none items-center gap-2.5 rounded-[var(--r-xs)] px-2.5 py-1.5 text-text2 outline-none transition-colors data-[highlighted]:bg-card2 data-[highlighted]:text-text"
      style={{ fontSize: "var(--t-label)" }}
    >
      <props.Icon size={14} strokeWidth={1.9} className="shrink-0 opacity-80" />
      {props.label}
    </ContextMenu.Item>
  );
}

export function TabMenu({ rel, children }: { rel: string; children: React.ReactNode }) {
  const ed = useEditor.getState;
  const ws = useWorkspace.getState;

  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger asChild>{children}</ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Content
          className="material-panel z-[120] min-w-[220px] rounded-[var(--r-md)] border border-border-w p-1"
          style={{ boxShadow: "var(--bevel-strong), var(--shadow-2)" }}
        >
          <Item Icon={X} label="Kapat" onSelect={() => ed().close(rel)} />
          <Item Icon={XCircle} label="Diğerlerini Kapat" onSelect={() => ed().closeOthers(rel)} />
          <Item Icon={ArrowRightToLine} label="Sağdakileri Kapat" onSelect={() => ed().closeRight(rel)} />
          <Item Icon={XCircle} label="Tümünü Kapat" onSelect={() => ed().closeAll()} />
          <ContextMenu.Separator className="mx-1 my-1 h-px bg-border-w" />
          <Item Icon={ClipboardCopy} label="Yolu Kopyala" onSelect={() => void ws().copyPath(rel)} />
          <Item Icon={ExternalLink} label="Sistemde Göster" onSelect={() => void ws().revealInOS(rel)} />
        </ContextMenu.Content>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}
