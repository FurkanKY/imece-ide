/* editor.js — Monaco tabanlı çok sekmeli editör + Python (QWebChannel) köprüsü.
   Python -> JS : window.API.* (view.page().runJavaScript ile çağrılır)
   JS -> Python : window.bridge.* (QWebChannel slot'ları)                        */

// Monaco'nun dil "worker"larını devre dışı bırak (file:// altında worker sorunlarını
// önler). Sözdizimi renklendirme ana iş parçacığında çalışır; sadece gelişmiş dil
// servisleri (tip kontrolü vb.) devre dışı kalır — Faz 1 için yeterli.
self.MonacoEnvironment = {
  getWorker: function () {
    const blob = new Blob(["self.onmessage=function(){}"], { type: "application/javascript" });
    return new Worker(URL.createObjectURL(blob));
  }
};

let editor = null;
let diffEditor = null;  // merkez inline diff editörü (Cursor deseni)
let mode = "editor";    // "editor" | "diff" — merkezde ne gösteriliyor
let diffTab = null;     // { path } veya null (tek diff sekmesi)
let bridge = null;
let ready = false;
const tabs = {};        // path -> { model, dirty }
let order = [];         // sekme sırası (path listesi)
let active = null;      // aktif path
const decorations = {}; // path -> debug satır dekorasyonu id'leri

function setMode(m) {
  mode = m;
  document.getElementById("editor").style.display = (m === "editor") ? "block" : "none";
  document.getElementById("diff").style.display = (m === "diff") ? "block" : "none";
}

function ensureDiffEditor() {
  if (!diffEditor) {
    diffEditor = monaco.editor.createDiffEditor(document.getElementById("diff"), {
      theme: "vs-dark", automaticLayout: true, renderSideBySide: false,  // inline
      readOnly: true, fontSize: 13, minimap: { enabled: false },
      scrollBeyondLastLine: false, glyphMargin: false
    });
  }
  return diffEditor;
}

const EXT_LANG = {
  py: "python", c: "c", h: "c", cpp: "cpp", cc: "cpp", hpp: "cpp",
  js: "javascript", mjs: "javascript", ts: "typescript", jsx: "javascript",
  json: "json", md: "markdown", html: "html", css: "css", sh: "shell",
  java: "java", go: "go", rs: "rust", rb: "ruby", php: "php", sql: "sql",
  yml: "yaml", yaml: "yaml", xml: "xml", txt: "plaintext"
};
function langOf(path) {
  const ext = (path.split(".").pop() || "").toLowerCase();
  return EXT_LANG[ext] || "plaintext";
}

// ---- QWebChannel köprüsü ----
new QWebChannel(qt.webChannelTransport, function (channel) {
  bridge = channel.objects.bridge;
  bootMonaco();
});

// ---- Monaco yükle ----
function bootMonaco() {
  require.config({ paths: { vs: "vs" } });
  require(["vs/editor/editor.main"], function () {
    editor = monaco.editor.create(document.getElementById("editor"), {
      value: "", language: "plaintext", theme: "vs-dark",
      automaticLayout: true, fontSize: 13, minimap: { enabled: true },
      glyphMargin: true, scrollBeyondLastLine: false
    });

    // İçerik değişince "dirty" işaretle
    editor.onDidChangeModelContent(function () {
      if (active && tabs[active] && !tabs[active].dirty) {
        tabs[active].dirty = true;
        renderTabs();
        if (bridge) bridge.dirtyChanged(active, true);
      }
    });

    // Ctrl+S -> kaydet
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, function () {
      window.API.saveActive();
    });

    // Gutter (glyph) tıklama -> breakpoint aç/kapa (Faz 3'te kullanılacak)
    editor.onMouseDown(function (e) {
      if (e.target.type === monaco.editor.MouseTargetType.GUTTER_GLYPH_MARGIN && active) {
        toggleBreakpoint(active, e.target.position.lineNumber);
      }
    });

    setMode("editor");
    ready = true;
    if (bridge) bridge.ready();
  });
}

// ---- Sekme çubuğu ----
function renderTabs() {
  const bar = document.getElementById("tabs");
  bar.innerHTML = "";
  order.forEach(function (path) {
    const t = document.createElement("div");
    t.className = "tab" + (path === active ? " active" : "") + (tabs[path].dirty ? " dirty" : "");
    const name = path.split("/").pop();
    t.innerHTML = '<span class="dot"></span><span class="name"></span><span class="x">✕</span>';
    t.querySelector(".name").textContent = name;
    t.title = path;
    t.addEventListener("click", function (ev) {
      if (ev.target.classList.contains("x")) { window.API.closeTab(path); }
      else { activate(path); }
    });
    bar.appendChild(t);
  });
  // diff sekmesi (varsa) — belirgin ⇄ ikonuyla
  if (diffTab) {
    const t = document.createElement("div");
    t.className = "tab difftab" + (mode === "diff" ? " active" : "");
    t.innerHTML = '<span class="difficon">⇄</span><span class="name"></span><span class="x">✕</span>';
    t.querySelector(".name").textContent = "Diff: " + diffTab.path.split("/").pop();
    t.title = "Değişiklik: " + diffTab.path;
    t.addEventListener("click", function (ev) {
      if (ev.target.classList.contains("x")) { window.API.closeDiff(); }
      else { setMode("diff"); active = null; renderTabs(); }
    });
    bar.appendChild(t);
  }
  const anyTab = order.length || diffTab;
  document.getElementById("empty").style.display = anyTab ? "none" : "flex";
}

function activate(path) {
  if (!tabs[path]) return;
  active = path;
  editor.setModel(tabs[path].model);
  setMode("editor");
  renderTabs();
  editor.focus();
}

// ---- Python'dan çağrılan API ----
window.API = {
  openFile: function (path, content, language) {
    if (!tabs[path]) {
      const model = monaco.editor.createModel(content, language || langOf(path));
      tabs[path] = { model: model, dirty: false };
      order.push(path);
    } else {
      // zaten açık: içeriği tazele (dirty değilse)
      if (!tabs[path].dirty) tabs[path].model.setValue(content);
    }
    activate(path);
  },
  setContent: function (path, content) {   // ajan diff'i uygulandıktan sonra tazele
    if (tabs[path]) {
      tabs[path].model.setValue(content);
      tabs[path].dirty = false;
      renderTabs();
    }
  },
  saveActive: function () {
    if (active && bridge) {
      bridge.fileSaved(active, tabs[active].model.getValue());
      tabs[active].dirty = false;
      renderTabs();
    }
  },
  markSaved: function (path) {
    if (tabs[path]) { tabs[path].dirty = false; renderTabs(); }
  },
  // --- Merkez inline diff (Cursor deseni): ajan önerisini tam boy göster ---
  openDiff: function (path, original, modified, language) {
    const de = ensureDiffEditor();
    const lang = language || langOf(path);
    const o = monaco.editor.createModel(original || "", lang);
    const m = monaco.editor.createModel(modified || "", lang);
    const old = de.getModel();
    de.setModel({ original: o, modified: m });
    if (old) { try { old.original.dispose(); old.modified.dispose(); } catch (e) {} }
    diffTab = { path: path };
    active = null;
    setMode("diff");
    renderTabs();
  },
  closeDiff: function () {
    diffTab = null;
    if (order.length) { active = order[order.length - 1]; editor.setModel(tabs[active].model); }
    setMode("editor");
    renderTabs();
  },
  closeTab: function (path) {
    if (!tabs[path]) return;
    tabs[path].model.dispose();
    delete tabs[path];
    order = order.filter(function (p) { return p !== path; });
    if (active === path) {
      active = order.length ? order[order.length - 1] : null;
      if (active) editor.setModel(tabs[active].model);
      else editor.setModel(null);
    }
    renderTabs();
  },
  // --- Faz 3 (debugger) için hazır ---
  showDebugLine: function (path, line) {
    if (!tabs[path]) return;
    activate(path);
    decorations[path] = editor.deltaDecorations(decorations[path] || [], [{
      range: new monaco.Range(line, 1, line, 1),
      options: { isWholeLine: true, className: "dbg-line",
                 glyphMarginClassName: "dbg-arrow" }
    }]);
    editor.revealLineInCenter(line);
  },
  clearDebugLine: function () {
    Object.keys(decorations).forEach(function (p) {
      if (tabs[p]) decorations[p] = editor.deltaDecorations(decorations[p], []);
    });
  },
  setBreakpoints: function (path, lines) {
    // görsel breakpoint dekorasyonları (Faz 3'te doldurulacak)
  }
};

const breakpoints = {};  // path -> Set(line)
function toggleBreakpoint(path, line) {
  if (!breakpoints[path]) breakpoints[path] = new Set();
  const s = breakpoints[path];
  if (s.has(line)) s.delete(line); else s.add(line);
  if (bridge) bridge.breakpointsChanged(path, JSON.stringify(Array.from(s).sort(function (a, b) { return a - b; })));
}
