/** Dosya yolu → Monaco dil kimliği. Monaco'dan bağımsız tutulur ki durum çubuğu
 * gibi hafif yüzeyler editör paketini ilk açılışta çekmesin. */
const EXT_LANG: Record<string, string> = {
  ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
  ".mjs": "javascript", ".json": "json", ".css": "css", ".scss": "scss", ".less": "less",
  ".html": "html", ".md": "markdown", ".py": "python", ".rs": "rust", ".go": "go",
  ".java": "java", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
  ".rb": "ruby", ".php": "php", ".sql": "sql", ".sh": "shell", ".yml": "yaml",
  ".yaml": "yaml", ".xml": "xml", ".toml": "ini", ".ini": "ini", ".cfg": "ini",
};

export function langForPath(path: string): string {
  const i = path.lastIndexOf(".");
  const ext = i >= 0 ? path.slice(i).toLowerCase() : "";
  return EXT_LANG[ext] ?? "plaintext";
}
