"""
webhost — yeni web-shell'in yerleşik (native) host katmanı.

Sorumluluk ayrımı (bkz. .claude/plans/web-shell-ui.md):
  scheme.py   app:// özel şeması (web/ui/dist servisი)
  bridge.py   QWebChannel RPC dispatcher (istek/yanıt + olay akışları)
  window.py   frameless pencere + tam-boy QWebEngineView + geometri kalıcılığı
  api/        domain handler'ları (app, settings, ... fazlarla genişler)

Motor modülleri (adapters/agents/runner/project/project_runner) DOKUNULMAZDIR;
bu paket onları yalnız sarmalar.
"""
