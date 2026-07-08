/* vfs.ts — mock bridge için küçük sanal dosya sistemi (tarayıcı geliştirmesi). */

export interface VNode {
  [name: string]: VNode | string; // klasör → VNode, dosya → içerik
}

export const VFS: VNode = {
  src: {
    "main.tsx": `import { createRoot } from "react-dom/client";\nimport App from "./App";\n\ncreateRoot(document.getElementById("root")!).render(<App />);\n`,
    "App.tsx": `export default function App() {\n  return <h1>Merhaba dünya</h1>;\n}\n`,
    components: {
      "Button.tsx": `export function Button({ label }: { label: string }) {\n  return <button>{label}</button>;\n}\n`,
    },
    "utils.ts": `export const clamp = (n: number, lo: number, hi: number) =>\n  Math.max(lo, Math.min(hi, n));\n`,
  },
  "README.md": `# Demo API\n\nMock bridge ile çalışan örnek proje.\n\n- \`src/\` kaynak\n- \`package.json\` bağımlılıklar\n`,
  "package.json": `{\n  "name": "demo-api",\n  "version": "1.0.0",\n  "type": "module"\n}\n`,
  ".env.example": `API_KEY=\nPORT=3000\n`,
};

function resolve(rel: string): VNode | string | undefined {
  if (!rel) return VFS;
  let cur: VNode | string = VFS;
  for (const part of rel.split("/")) {
    if (typeof cur === "string") return undefined;
    cur = cur[part];
    if (cur === undefined) return undefined;
  }
  return cur;
}

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

export function listDir(rel: string) {
  const node = resolve(rel);
  if (typeof node !== "object" || node === undefined) return { entries: [] };
  const entries = Object.entries(node).map(([name, val]) => ({
    name,
    rel: rel ? `${rel}/${name}` : name,
    isDir: typeof val === "object",
    ext: typeof val === "object" ? "" : extOf(name),
  }));
  entries.sort((a, b) =>
    a.isDir !== b.isDir ? (a.isDir ? -1 : 1) : a.name.localeCompare(b.name),
  );
  return { entries };
}

export function readFile(rel: string) {
  const node = resolve(rel);
  if (typeof node !== "string") return { content: "", truncated: false };
  return { content: node, truncated: false };
}

export function writeFile(rel: string, content: string) {
  const parts = rel.split("/");
  const name = parts.pop()!;
  let cur = VFS;
  for (const p of parts) {
    if (typeof cur[p] !== "object") cur[p] = {};
    cur = cur[p] as VNode;
  }
  cur[name] = content;
}

export function createNode(rel: string, isDir: boolean) {
  const parts = rel.split("/");
  const name = parts.pop()!;
  let cur = VFS;
  for (const p of parts) {
    if (typeof cur[p] !== "object") cur[p] = {};
    cur = cur[p] as VNode;
  }
  if (cur[name] !== undefined) throw new Error(`Zaten var: ${rel}`);
  cur[name] = isDir ? {} : "";
}

export function renameNode(rel: string, newName: string): string {
  const parts = rel.split("/");
  const oldName = parts.pop()!;
  let cur = VFS;
  for (const p of parts) cur = cur[p] as VNode;
  if (cur[newName] !== undefined) throw new Error(`Zaten var: ${newName}`);
  cur[newName] = cur[oldName];
  delete cur[oldName];
  return [...parts, newName].join("/");
}

export function moveNode(rel: string, newDir: string): string {
  if (newDir === rel || newDir.startsWith(rel + "/")) {
    throw new Error("Klasör kendi altına taşınamaz.");
  }
  const node = resolve(rel);
  if (node === undefined) throw new Error(`Yok: ${rel}`);
  const target = resolve(newDir);
  if (typeof target !== "object" || target === undefined) throw new Error(`Klasör değil: ${newDir}`);
  const parts = rel.split("/");
  const name = parts.pop()!;
  if (target[name] !== undefined) throw new Error(`Hedefte zaten var: ${name}`);
  let cur = VFS;
  for (const p of parts) cur = cur[p] as VNode;
  target[name] = node;
  delete cur[name];
  return newDir ? `${newDir}/${name}` : name;
}

export function deleteNode(rel: string) {
  const parts = rel.split("/");
  const name = parts.pop()!;
  let cur = VFS;
  for (const p of parts) cur = cur[p] as VNode;
  delete cur[name];
}

export function listAllFiles(rel = "", acc: string[] = []): string[] {
  const node = resolve(rel);
  if (typeof node !== "object" || node === undefined) return acc;
  for (const [name, val] of Object.entries(node)) {
    const child = rel ? `${rel}/${name}` : name;
    if (typeof val === "object") listAllFiles(child, acc);
    else acc.push(child);
  }
  return acc;
}
