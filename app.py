"""
app.py
------
Basit web arayüzü (Flask). Tarayıcıdan sistemi kontrol etmeni sağlar:
  - görev yaz
  - her role (planner/coder/reviewer) hangi modeli kullanacağını seç
  - tur sayısı ve "kodu çalıştır" seçeneği
  - akışı ve metrikleri canlı izle

Çalıştır:  python app.py   ->  http://127.0.0.1:5000
"""

import json

from dotenv import load_dotenv
from flask import Flask, request, Response, render_template

from runner import run_task

load_dotenv()
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(force=True)
    task = (data.get("task") or "").strip()
    routing = {
        "planner": data.get("planner", "claude"),
        "coder": data.get("coder", "deepseek"),
        "reviewer": data.get("reviewer", "gemini"),
    }
    max_rounds = int(data.get("max_rounds", 3))
    run_python = bool(data.get("run_python", False))

    def stream():
        if not task:
            yield json.dumps({"type": "error", "text": "Görev boş."}) + "\n"
            return
        try:
            for ev in run_task(task, routing, max_rounds, run_python):
                yield json.dumps(ev, ensure_ascii=False) + "\n"
        except Exception as e:  # hataları da arayüze taşı
            yield json.dumps({"type": "error", "text": str(e)}) + "\n"

    # NDJSON: her satır bir olay. Tarayıcı satır satır okur.
    return Response(stream(), mimetype="application/x-ndjson")


if __name__ == "__main__":
    # use_reloader=False: aksi halde her çalıştırmada yazılan output/result.py
    # dosyası reloader'ı tetikleyip devam eden isteği koparıyor.
    app.run(debug=True, use_reloader=False, threaded=True, port=5000)
