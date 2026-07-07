/* mock — düz tarayıcıda geliştirme + görsel doğrulama için sahte host.
   Senaryo seçimi: ?scenario=empty|project|running|result|error (P2'de fixtures/run.ts genişler). */

import { Api, Bridge, Events, Prefs } from "../protocol";
import * as vfs from "./vfs";

const DEFAULT_PREFS: Prefs = {
  accent: "blue",
  density: "comfortable",
  enterToSend: true,
  animations: true,
  lastProject: null,
  recentProjects: [
    { path: "C:/Projeler/multi-agent", name: "multi-agent", lastOpened: "2026-07-05T12:00:00Z" },
    { path: "C:/Projeler/demo-api", name: "demo-api", lastOpened: "2026-07-01T09:30:00Z" },
  ],
};

export class MockBridge implements Bridge {
  readonly isNative = false;
  private listeners = new Map<string, Set<(payload: unknown) => void>>();
  private maximized = false;
  private prefs: Prefs = (() => {
    try {
      const raw = localStorage.getItem("magent.prefs");
      return raw ? { ...DEFAULT_PREFS, ...JSON.parse(raw) } : DEFAULT_PREFS;
    } catch {
      return DEFAULT_PREFS;
    }
  })();

  get scenario(): string {
    return new URLSearchParams(location.search).get("scenario") ?? "empty";
  }

  private emit<C extends keyof Events>(channel: C, payload: Events[C]) {
    this.listeners.get(channel)?.forEach((cb) => cb(payload));
  }

  async call<M extends keyof Api>(method: M, params: Api[M]["params"]): Promise<Api[M]["result"]> {
    await new Promise((r) => setTimeout(r, 15)); // köprü gecikmesi hissi
    type R = Api[M]["result"];
    switch (method) {
      case "window.minimize":
      case "window.close":
      case "window.startSystemMove":
      case "window.startSystemResize":
        return {} as R;
      case "window.toggleMaximize":
        this.maximized = !this.maximized;
        this.emit("window.state", { maximized: this.maximized, focused: true });
        return { maximized: this.maximized } as R;
      case "app.info":
        return { version: "0.1.0-mock", platform: "browser", chromium: "-" } as R;
      case "app.pickFolder":
        return { path: "C:/Projeler/demo-api" } as R;
      case "project.open":
        return { root: (params as { path: string }).path, name: "demo-api" } as R;
      case "project.listFiles":
        return { files: vfs.listAllFiles() } as R;
      case "fs.listDir":
        return vfs.listDir((params as { rel: string }).rel) as R;
      case "fs.readFile":
        return vfs.readFile((params as { rel: string }).rel) as R;
      case "fs.writeFile": {
        const p = params as { rel: string; content: string };
        vfs.writeFile(p.rel, p.content);
        return {} as R;
      }
      case "settings.get":
        return this.prefs as R;
      case "settings.set":
        this.prefs = params as Prefs;
        localStorage.setItem("magent.prefs", JSON.stringify(this.prefs));
        return {} as R;
      default:
        console.warn("[mock] karşılıksız metot:", method, params);
        return {} as R;
    }
  }

  on<C extends keyof Events>(channel: C, cb: (payload: Events[C]) => void): () => void {
    let set = this.listeners.get(channel);
    if (!set) {
      set = new Set();
      this.listeners.set(channel, set);
    }
    set.add(cb as (payload: unknown) => void);
    return () => set!.delete(cb as (payload: unknown) => void);
  }
}
