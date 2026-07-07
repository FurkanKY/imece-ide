/* Köprü seçimi: Qt taşıyıcısı varsa gerçek host, yoksa tarayıcı mock'u. */

import { Bridge } from "./protocol";
import { QtBridge } from "./qt";
import { MockBridge } from "./mock";

export const bridge: Bridge =
  typeof window !== "undefined" && window.qt?.webChannelTransport
    ? new QtBridge()
    : new MockBridge();

export * from "./protocol";
