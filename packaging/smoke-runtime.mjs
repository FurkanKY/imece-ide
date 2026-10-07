import path from "node:path";
import { statSync } from "node:fs";

export function selectLinuxPlatform(host = process.env) {
  const requested = host.QT_QPA_PLATFORM;
  if (requested && !["xcb", "wayland", "offscreen"].includes(requested)) {
    throw new Error("QT_QPA_PLATFORM must be xcb, wayland, or offscreen");
  }
  const runtime = host.XDG_RUNTIME_DIR;
  const display = host.WAYLAND_DISPLAY;
  let waylandSocket;
  if (runtime && path.isAbsolute(runtime) && display) {
    waylandSocket = path.isAbsolute(display) ? display : path.join(runtime, display);
    try { if (!statSync(waylandSocket).isSocket()) waylandSocket = undefined; }
    catch { waylandSocket = undefined; }
  }
  if (requested === "wayland" && !waylandSocket) throw new Error("Requested Wayland platform has no available host socket");
  if (requested === "xcb") return { platform: "xcb", display: host.DISPLAY, xauthority: host.XAUTHORITY };
  if (requested === "wayland") return { platform: "wayland", waylandDisplay: waylandSocket };
  if (requested === "offscreen") return { platform: "offscreen" };
  if (waylandSocket) return { platform: "wayland", waylandDisplay: waylandSocket };
  if (host.DISPLAY) return { platform: "xcb", display: host.DISPLAY, xauthority: host.XAUTHORITY };
  return { platform: "offscreen" };
}

export function linuxSmokeEnvironment(scratch, host = process.env) {
  const selected = selectLinuxPlatform(host);
  return {
    PATH: path.join(scratch, "path"), HOME: path.join(scratch, "home"),
    XDG_DATA_HOME: path.join(scratch, "local"), XDG_CONFIG_HOME: path.join(scratch, "roaming"),
    XDG_CACHE_HOME: path.join(scratch, "temp"), XDG_RUNTIME_DIR: path.join(scratch, "runtime"),
    TMPDIR: path.join(scratch, "temp"), SHELL: "/bin/sh", LANG: "C.UTF-8",
    QT_QPA_PLATFORM: selected.platform,
    ...(selected.display ? { DISPLAY: selected.display } : {}),
    ...(selected.xauthority ? { XAUTHORITY: selected.xauthority } : {}),
    ...(selected.waylandDisplay ? { WAYLAND_DISPLAY: selected.waylandDisplay } : {}),
  };
}

export function boundedText(current, chunk, limit = 12000) {
  const remaining = limit - current.length;
  if (remaining <= 0) return current;
  return current + chunk.toString("utf8", 0, Math.min(chunk.length, remaining));
}
