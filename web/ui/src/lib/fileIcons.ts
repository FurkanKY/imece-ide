/* fileIcons.ts — uzantı → lucide ikonu + renk. theme.py FILE_ICONS portu.
   Renkler token değil (dil kimliği); ama tek yerde toplu, kolayca ayarlanır. */

import {
  FileCode, FileJson, FileText, FileType, FileCog, FileTerminal, KeyRound,
  Braces, Hash, Database, type LucideIcon,
} from "lucide-react";

interface IconSpec {
  Icon: LucideIcon;
  color: string;
}

const MAP: Record<string, IconSpec> = {
  ".py": { Icon: FileCode, color: "#4f8cff" },
  ".ts": { Icon: FileType, color: "#4f8cff" },
  ".tsx": { Icon: FileType, color: "#4f8cff" },
  ".js": { Icon: FileCode, color: "#e6c74f" },
  ".jsx": { Icon: FileCode, color: "#e6c74f" },
  ".mjs": { Icon: FileCode, color: "#e6c74f" },
  ".json": { Icon: FileJson, color: "#e6a23c" },
  ".md": { Icon: FileText, color: "#8a8a92" },
  ".html": { Icon: FileCode, color: "#ef5b5b" },
  ".css": { Icon: Hash, color: "#4f8cff" },
  ".scss": { Icon: Hash, color: "#cf649a" },
  ".c": { Icon: FileCode, color: "#5c9fff" },
  ".h": { Icon: FileCode, color: "#5c9fff" },
  ".cpp": { Icon: FileCode, color: "#5c9fff" },
  ".go": { Icon: FileCode, color: "#4fd0e6" },
  ".rs": { Icon: FileCode, color: "#c98a5b" },
  ".rb": { Icon: FileCode, color: "#ef5b5b" },
  ".php": { Icon: FileCode, color: "#8a8ad6" },
  ".java": { Icon: FileCode, color: "#e6a23c" },
  ".sql": { Icon: Database, color: "#4fd0e6" },
  ".sh": { Icon: FileTerminal, color: "#31c774" },
  ".yml": { Icon: FileCog, color: "#8a8a92" },
  ".yaml": { Icon: FileCog, color: "#8a8a92" },
  ".toml": { Icon: FileCog, color: "#8a8a92" },
  ".cfg": { Icon: FileCog, color: "#8a8a92" },
  ".ini": { Icon: FileCog, color: "#8a8a92" },
  ".xml": { Icon: Braces, color: "#8a8a92" },
  ".env": { Icon: KeyRound, color: "#e6a23c" },
  ".txt": { Icon: FileText, color: "#8a8a92" },
};

const DEFAULT: IconSpec = { Icon: FileText, color: "#8a8a92" };

export function fileIcon(name: string): IconSpec {
  const i = name.lastIndexOf(".");
  const ext = i >= 0 ? name.slice(i).toLowerCase() : "";
  return MAP[ext] ?? DEFAULT;
}
