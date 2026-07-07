/* qt.ts — gerçek QWebChannel taşıyıcısı üzerinden Bridge implementasyonu.
   qwebchannel.js Qt'nin kendi kopyasından yüklenir (qrc:// — web/editor'da kanıtlı desen). */

import { Api, Bridge, BridgeError, Events } from "./protocol";

declare global {
  interface Window {
    qt?: { webChannelTransport: unknown };
    QWebChannel?: new (
      transport: unknown,
      cb: (channel: { objects: { host: QtHostObject } }) => void,
    ) => void;
  }
}

interface QtHostObject {
  call(raw: string): void;
  reply: { connect(cb: (raw: string) => void): void };
  event: { connect(cb: (raw: string) => void): void };
}

function loadQWebChannelJs(): Promise<void> {
  if (window.QWebChannel) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "qrc:///qtwebchannel/qwebchannel.js";
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("qwebchannel.js yüklenemedi"));
    document.head.appendChild(s);
  });
}

export class QtBridge implements Bridge {
  readonly isNative = true;
  private host!: QtHostObject;
  private ready: Promise<void>;
  private nextId = 1;
  private pending = new Map<
    number,
    { resolve: (v: unknown) => void; reject: (e: Error) => void }
  >();
  private listeners = new Map<string, Set<(payload: unknown) => void>>();

  constructor() {
    this.ready = this.connect();
  }

  private async connect(): Promise<void> {
    await loadQWebChannelJs();
    await new Promise<void>((resolve) => {
      new window.QWebChannel!(window.qt!.webChannelTransport, (channel) => {
        this.host = channel.objects.host;
        this.host.reply.connect((raw) => this.onReply(raw));
        this.host.event.connect((raw) => this.onEvent(raw));
        resolve();
      });
    });
  }

  private onReply(raw: string) {
    const msg = JSON.parse(raw) as {
      id: number;
      ok: boolean;
      result?: unknown;
      error?: { code: string; message: string };
    };
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    if (msg.ok) p.resolve(msg.result ?? {});
    else p.reject(new BridgeError(msg.error?.code ?? "unknown", msg.error?.message ?? "Bilinmeyen hata"));
  }

  private onEvent(raw: string) {
    const msg = JSON.parse(raw) as { channel: string; payload: unknown };
    this.listeners.get(msg.channel)?.forEach((cb) => cb(msg.payload));
  }

  async call<M extends keyof Api>(method: M, params: Api[M]["params"]): Promise<Api[M]["result"]> {
    await this.ready;
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.host.call(JSON.stringify({ id, method, params }));
    });
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
