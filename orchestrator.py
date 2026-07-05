"""
orchestrator.py
---------------
Terminal (komut satırı) arayüzü. Arayüz istemezsen bunu kullan.

    python orchestrator.py "dosya kopyalayan bir python scripti yaz"
    python orchestrator.py "..." --run     # üretilen kodu gerçekten çalıştır
"""

import sys
from dotenv import load_dotenv

from runner import run_task

load_dotenv()

# Windows terminali Türkçe (cp1254) olabildiği için çıktıyı UTF-8'e sabitle.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print('Kullanım: python orchestrator.py "görev" [--run]')
        sys.exit(1)
    task = args[0]
    run_python = "--run" in sys.argv

    for ev in run_task(task, run_python=run_python):
        t = ev["type"]
        if t == "stage":
            print("\n" + "=" * 60)
            print(f"  {ev['stage'].upper()}  ({ev['provider']})")
            print("=" * 60)
        elif t == "output":
            print(ev["text"])
        elif t == "exec":
            print(f"\n[ÇALIŞTIRMA] {'OK' if ev['ok'] else 'HATA'}: {ev['text'][:400]}")
        elif t == "metric":
            print(f"   ⏱ {ev['latency_s']}s · {ev['tokens']} token · ${ev['cost_usd']}")
        elif t == "note":
            print("\n" + ev["text"])
        elif t == "done":
            tt = ev["totals"]
            print("\n" + "=" * 60)
            print(f"BİTTİ → {ev['code_path']}")
            print(f"TOPLAM: {tt['latency_s']}s · {tt['tokens']} token · ${tt['cost_usd']}")
            print("=" * 60)


if __name__ == "__main__":
    main()
