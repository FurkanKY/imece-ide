"""
runner.py
---------
Orkestratör. Ajanları sırayla çağırır ve her adımı bir "olay" (event) olarak
YAYINLAR (yield eder). Böylece hem terminal hem web arayüzü aynı akışı canlı
gösterebilir.

İçerdiği optimizasyonlar:
  #3 Execution grounding : üretilen Python kodunu gerçekten çalıştırır; hata
     çıkarsa gerçek traceback'i Coder'a geri besler.
  #4 Routing            : her role hangi modelin atandığı dışarıdan gelir.
  #6 Observability      : her adımın süresi, token'ı ve maliyeti ölçülür.
"""

import os
import re
import sys
import subprocess
import tempfile

from agents import build_agents

MAX_ROUNDS_DEFAULT = 3
OUTPUT_DIR = "output"


def extract_code(text: str) -> str:
    """Cevaptaki ```...``` kod bloğunu ayıkla; yoksa metnin tamamını döndür."""
    m = re.search(r"```(?:\w+)?\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def run_python_code(code: str, execute: bool):
    """Kodu güvenlik için önce derler (compile), istenirse çalıştırır.
    Döner: (ok: bool, çıktı/hata metni: str)."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    )
    tmp.write(code)
    tmp.close()

    # Alt-sürece UTF-8 dayat: aksi halde Windows Türkçe (cp1254) çıktı üretir,
    # utf-8 çözme çöker ve gerçek çıktı/hata sessizce kaybolur. errors="replace"
    # ise her ihtimale karşı çözmenin asla patlamamasını sağlar.
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    kw = dict(capture_output=True, text=True, encoding="utf-8",
              errors="replace", env=child_env, timeout=30)
    try:
        # 1) Derleme kontrolü (güvenli, kodu çalıştırmaz — sadece söz dizimi).
        comp = subprocess.run([sys.executable, "-m", "py_compile", tmp.name], **kw)
        if comp.returncode != 0:
            return False, f"[Derleme hatası]\n{comp.stderr}"

        if not execute:
            return True, "[Derleme başarılı] (çalıştırma kapalı)"

        # 2) Gerçek çalıştırma (zaman aşımıyla).
        run = subprocess.run([sys.executable, tmp.name], **kw)
        if run.returncode != 0:
            return False, f"[Çalışma hatası]\n{run.stderr or run.stdout}"
        return True, f"[Çalıştı ✔]\n{run.stdout}"
    except subprocess.TimeoutExpired:
        return False, "[Zaman aşımı] Kod 30 saniyede bitmedi."
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def run_task(task, routing=None, max_rounds=MAX_ROUNDS_DEFAULT, run_python=False):
    """Ana akış — olaylar üretir (generator)."""
    agents = build_agents(routing)
    totals = {"cost_usd": 0.0, "latency_s": 0.0, "tokens": 0}

    def track(resp, stage):
        totals["cost_usd"] += resp.cost_usd
        totals["latency_s"] += resp.latency_s
        totals["tokens"] += resp.prompt_tokens + resp.completion_tokens
        return {
            "type": "metric", "stage": stage,
            "provider": resp.provider, "model": resp.model,
            "latency_s": round(resp.latency_s, 2),
            "tokens": resp.prompt_tokens + resp.completion_tokens,
            "cost_usd": round(resp.cost_usd, 5),
        }

    # 1) PLAN
    yield {"type": "stage", "stage": "plan", "provider": agents["planner"].provider}
    plan = agents["planner"].run(f"Görev: {task}")
    yield track(plan, "plan")
    yield {"type": "output", "stage": "plan", "text": plan.text}

    # 2) İLK KOD
    yield {"type": "stage", "stage": "code", "provider": agents["coder"].provider}
    resp = agents["coder"].run(f"Görev: {task}\n\nPlan:\n{plan.text}")
    yield track(resp, "code")
    code = extract_code(resp.text)
    yield {"type": "output", "stage": "code", "text": code}

    # 3-4) Döngü: çalıştır + incele + düzelt
    for rnd in range(1, max_rounds + 1):
        # 3a) Execution grounding
        ok, exec_out = run_python_code(code, execute=run_python)
        yield {"type": "exec", "ok": ok, "text": exec_out, "round": rnd}

        # 3b) İnceleme
        yield {"type": "stage", "stage": "review", "provider": agents["reviewer"].provider, "round": rnd}
        review = agents["reviewer"].run(f"Görev: {task}\n\nKod:\n{code}\n\nÇalıştırma sonucu:\n{exec_out}")
        yield track(review, "review")
        yield {"type": "output", "stage": "review", "text": review.text, "round": rnd}

        approved = "APPROVED" in review.text.upper()
        if ok and approved:
            yield {"type": "note", "text": f"✅ Onaylandı (tur {rnd})."}
            break
        if rnd == max_rounds:
            yield {"type": "note", "text": "⚠️ Maksimum tura ulaşıldı, son kod kullanılacak."}
            break

        # 4) Düzeltme — hem çalıştırma hatasını hem inceleme notunu geri besle
        yield {"type": "stage", "stage": "fix", "provider": agents["coder"].provider, "round": rnd}
        resp = agents["coder"].run(
            f"Görev: {task}\n\nÖnceki kod:\n{code}\n\n"
            f"Çalıştırma sonucu:\n{exec_out}\n\nİnceleme geri bildirimi:\n{review.text}"
        )
        yield track(resp, "fix")
        code = extract_code(resp.text)
        yield {"type": "output", "stage": "fix", "text": code, "round": rnd}

    # 5) Kaydet
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "result.py")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(code + "\n")

    totals["cost_usd"] = round(totals["cost_usd"], 5)
    totals["latency_s"] = round(totals["latency_s"], 2)
    yield {"type": "done", "code_path": out_path, "totals": totals, "code": code}
