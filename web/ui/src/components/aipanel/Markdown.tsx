/* Markdown — model çıktısı için markdown işleyici (P6.2).
   Kod blokları Monaco'nun colorize'ıyla temaya uygun renklenir (ekstra
   highlighter bağımlılığı yok — Monaco zaten gemide). Tipografi: base.css `.chat-md`. */

import { memo, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Copy, Check } from "lucide-react";
import { initMonaco } from "@/lib/monaco";
import { bridge } from "@/bridge";

/* ```lang etiketi → monaco dil kimliği (langForPath uzantı bazlı; burada ad bazlı) */
const LANG_ALIAS: Record<string, string> = {
  py: "python", js: "javascript", ts: "typescript", tsx: "typescript",
  jsx: "javascript", sh: "shell", bash: "shell", yml: "yaml", md: "markdown",
  "c++": "cpp", cs: "csharp", diff: "plaintext",
};

function CodeBlock({ lang, code }: { lang: string; code: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    const monaco = initMonaco();
    const id = LANG_ALIAS[lang] ?? lang ?? "plaintext";
    monaco.editor
      .colorize(code, id, { tabSize: 4 })
      .then((h) => { if (alive) setHtml(h); })
      .catch(() => { if (alive) setHtml(null); });
    return () => { alive = false; };
  }, [lang, code]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
    } catch {
      await bridge.call("app.clipboardWrite", { text: code });
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div className="group/code relative my-1.5 overflow-hidden rounded-[var(--r-sm)] border border-border-w bg-field">
      <button
        onClick={copy}
        title="Kopyala"
        aria-label="Kodu kopyala"
        className="icon-btn absolute right-1.5 top-1.5 z-10 size-6 opacity-0 transition-opacity focus-visible:opacity-100 group-hover/code:opacity-100"
      >
        {copied ? <Check size={12} className="text-ok" /> : <Copy size={12} />}
      </button>
      {html ? (
        <pre
          className="selectable overflow-x-auto px-3 py-2"
          style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-mono)", lineHeight: 1.55 }}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ) : (
        <pre
          className="selectable overflow-x-auto whitespace-pre px-3 py-2 text-text2"
          style={{ fontFamily: "var(--font-mono)", fontSize: "var(--t-mono)", lineHeight: 1.55 }}
        >
          {code}
        </pre>
      )}
    </div>
  );
}

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="chat-md selectable" style={{ fontSize: "var(--t-body)" }}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ className, children, ...props }) {
            const m = /language-(\S+)/.exec(className ?? "");
            const raw = String(children ?? "");
            // blok kod: language-x sınıfı VEYA çok satırlı içerik
            if (m || raw.includes("\n")) {
              return <CodeBlock lang={m?.[1] ?? "plaintext"} code={raw.replace(/\n$/, "")} />;
            }
            return <code {...props}>{raw}</code>;
          },
          // pre'yi CodeBlock zaten sarıyor — çift sarmayı önle
          pre({ children }) {
            return <>{children}</>;
          },
          a({ href, children }) {
            return (
              <a
                href={href}
                onClick={(e) => {
                  e.preventDefault();
                  if (href) void bridge.call("app.openExternal", { url: href });
                }}
              >
                {children}
              </a>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});
