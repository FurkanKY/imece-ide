/* ExplorerMenu — gezgin sağ-tık bağlam menüsü (Radix ContextMenu, temalı).
   Eski desktop.py:_tree_menu'nün halefi; native menü yerine token'lı overlay. */

import { ContextMenu } from "radix-ui";
import {
  FilePlus2, FolderPlus, PenLine, Trash2, ClipboardCopy, ExternalLink,
  type LucideIcon,
} from "lucide-react";
import { DirEntry } from "@/bridge";
import { useWorkspace } from "@/state/workspace";

function Item(props: {
  Icon: LucideIcon;
  label: string;
  danger?: boolean;
  onSelect: () => void;
}) {
  return (
    <ContextMenu.Item
      onSelect={props.onSelect}
      className={
        "flex cursor-default select-none items-center gap-2.5 rounded-[var(--r-xs)] px-2.5 py-1.5 outline-none transition-colors " +
        (props.danger
          ? "text-err data-[highlighted]:bg-err/15"
          : "text-text2 data-[highlighted]:bg-card2 data-[highlighted]:text-text")
      }
      style={{ fontSize: "var(--t-label)" }}
    >
      <props.Icon size={14} strokeWidth={1.9} className="shrink-0 opacity-80" />
      {props.label}
    </ContextMenu.Item>
  );
}

function Sep() {
  return <ContextMenu.Separator className="mx-1 my-1 h-px bg-border-w" />;
}

/** entry=null → boş alan (kök) menüsü */
export function ExplorerMenu({
  entry,
  children,
}: {
  entry: DirEntry | null;
  children: React.ReactNode;
}) {
  const ws = useWorkspace();
  // hedef klasör: klasöre tıklandıysa kendisi, dosyaysa üst klasörü, boş alansa kök
  const dirTarget = entry
    ? entry.isDir
      ? entry.rel
      : entry.rel.split("/").slice(0, -1).join("/")
    : "";

  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger asChild>{children}</ContextMenu.Trigger>
      <ContextMenu.Portal>
        <ContextMenu.Content
          className="material-panel z-[120] min-w-[210px] rounded-[var(--r-md)] border border-border-w p-1"
          style={{ boxShadow: "var(--bevel-strong), var(--shadow-2)" }}
        >
          <Item Icon={FilePlus2} label="Yeni Dosya" onSelect={() => void ws.newFile(dirTarget)} />
          <Item Icon={FolderPlus} label="Yeni Klasör" onSelect={() => void ws.newFolder(dirTarget)} />
          {entry && (
            <>
              <Sep />
              <Item Icon={PenLine} label="Yeniden Adlandır" onSelect={() => void ws.renameEntry(entry)} />
              <Item Icon={Trash2} label="Sil" danger onSelect={() => void ws.deleteEntry(entry)} />
              <Sep />
              <Item Icon={ClipboardCopy} label="Yolu Kopyala" onSelect={() => void ws.copyPath(entry.rel)} />
              <Item Icon={ExternalLink} label="Sistemde Göster" onSelect={() => void ws.revealInOS(entry.rel)} />
            </>
          )}
        </ContextMenu.Content>
      </ContextMenu.Portal>
    </ContextMenu.Root>
  );
}
