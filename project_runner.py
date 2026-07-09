"""
project_runner.py
-----------------
Var olan bir LOKAL PROJE üzerinde çalışan orkestratör.

Akış:
  1. Planner (Claude): proje dosya listesini görür → kısa plan + gereken
     dosyaların listesini ('FILES:' bölümü) üretir.
  2. İstenen dosyalar okunur ve Coder'a bağlam olarak verilir.
  3. Coder (DeepSeek): değişen/yeni her dosyanın TAM içeriğini
     '### FILE: yol' bloklarıyla üretir.
  4. Bloklar ayrıştırılır → her dosya için DIFF hesaplanır.
  5. Reviewer (Gemini): önerilen diff'i inceler.
  6. Sonuç 'proposal' olayı olarak yayınlanır; UYGULAMA kullanıcı onayına bağlıdır
     (arayüz Project.apply çağırır).

Her adım bir olay (event) olarak yield edilir → arayüz canlı gösterir.
"""

import re

from project import Project
from agents import build_agents

# Planner cevabındaki "FILES:" bölümünden dosya yollarını çıkar.
_FILES_RE = re.compile(r"FILES:\s*(.+)", re.DOTALL | re.IGNORECASE)
# Coder cevabındaki "### FILE: yol" + kod bloklarını çıkar.
_BLOCK_RE = re.compile(
    r"###\s*FILE:\s*(?P<path>[^\n`]+)\s*```[^\n]*\n(?P<body>.*?)```",
    re.DOTALL,
)
# Reviewer kararı: VERDICT: APPROVED | NEEDS_FIX
_VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVED|NEEDS[_ ]?FIX)", re.IGNORECASE)


def _parse_verdict(text: str) -> str | None:
    m = _VERDICT_RE.search(text or "")
    if not m:
        return None
    return "NEEDS_FIX" if "NEEDS" in m.group(1).upper() else "APPROVED"


def _parse_requested_files(text: str, valid: set[str]) -> list[str]:
    m = _FILES_RE.search(text)
    if not m:
        return []
    wanted = []
    for line in m.group(1).splitlines():
        p = line.strip().lstrip("-*0123456789. ").strip().strip("`").replace("\\", "/")
        if p and p in valid and p not in wanted:
            wanted.append(p)
    return wanted[:8]


def _plan_summary(text: str) -> str:
    """Planner'ın kullanıcıya dönük planını FILES protokol kısmından ayır."""
    return _FILES_RE.split(text or "", maxsplit=1)[0].strip()


def _parse_file_blocks(text: str) -> dict[str, str]:
    changes = {}
    for m in _BLOCK_RE.finditer(text):
        path = m.group("path").strip().strip("`").replace("\\", "/")
        changes[path] = m.group("body").rstrip() + "\n"
    return changes


def run_project_task(project_root, task, routing=None):
    proj = Project(project_root)
    agents = build_agents(routing)
    totals = {"cost_usd": 0.0, "latency_s": 0.0, "tokens": 0}

    def track(resp, stage):
        totals["cost_usd"] += resp.cost_usd
        totals["latency_s"] += resp.latency_s
        totals["tokens"] += resp.prompt_tokens + resp.completion_tokens
        return {"type": "metric", "stage": stage, "provider": resp.provider,
                "model": resp.model, "latency_s": round(resp.latency_s, 2),
                "tokens": resp.prompt_tokens + resp.completion_tokens,
                "cost_usd": round(resp.cost_usd, 5)}

    files = proj.list_files()
    valid = set(files)
    tree = "\n".join(files)
    yield {"type": "info", "text": f"Projede {len(files)} dosya bulundu."}

    # 1) PLAN + hangi dosyalar lazım?
    yield {"type": "stage", "stage": "plan", "provider": agents["planner"].provider}
    plan = agents["planner"].run(
        f"Görev: {task}\n\nProje dosyaları:\n{tree}\n\n"
        "Önce kısa bir plan yaz. EN SONDA 'FILES:' satırı koy ve altına, bu görev "
        "için okunması/düzenlenmesi gereken dosya yollarını (yukarıdaki listeden, "
        "en fazla 8) her satıra bir tane yaz."
    )
    yield track(plan, "plan")
    yield {"type": "output", "stage": "plan", "text": plan.text}

    wanted = _parse_requested_files(plan.text, valid)
    # UI bunu opsiyonel, yapılandırılmış bir plan olarak kullanır. Eski istemciler
    # olayı görmezden geldiğinde output + info akışı aynı biçimde devam eder.
    yield {"type": "plan", "summary": _plan_summary(plan.text), "files": wanted}
    yield {"type": "info", "text": "Okunacak dosyalar: " + (", ".join(wanted) or "(yok)")}

    # 2) İlgili dosyaları oku
    context_parts = []
    for rel in wanted:
        context_parts.append(f"### FILE: {rel}\n```\n{proj.read_file(rel)}\n```")
    context = "\n\n".join(context_parts) if context_parts else "(dosya seçilmedi)"

    # 3) CODER: değişen dosyaların tam içeriği
    yield {"type": "stage", "stage": "code", "provider": agents["coder"].provider}
    resp = agents["coder"].run(
        f"Görev: {task}\n\nPlan:\n{plan.text}\n\nMevcut dosyalar:\n{context}\n\n"
        "Değiştirilmesi veya oluşturulması gereken HER dosya için TAM yeni içeriği "
        "şu formatta ver (kısmi değil, dosyanın tamamı):\n"
        "### FILE: yol/dosya.uzanti\n```\n<dosyanın tam yeni içeriği>\n```\n"
        "Sadece gerçekten değişen/yeni dosyaları ver. Başka açıklama yazma."
    )
    yield track(resp, "code")
    changes = _parse_file_blocks(resp.text)

    if not changes:
        yield {"type": "info", "text": "⚠️ Coder ayrıştırılabilir bir dosya bloğu üretmedi."}
        yield {"type": "output", "stage": "code", "text": resp.text}

    # 4) Diff'leri hesapla
    proposals = []
    for rel, new in changes.items():
        diff = proj.make_diff(rel, new)
        is_new = not proj.exists(rel)
        proposals.append({"path": rel, "new": new, "diff": diff, "is_new": is_new})
        yield {"type": "diff", "path": rel, "is_new": is_new,
               "diff": diff or "(içerik aynı — değişiklik yok)"}

    # 5) REVIEWER: önerilen diff'i incele
    verdict = None
    if proposals:
        all_diffs = "\n\n".join(p["diff"] for p in proposals)
        yield {"type": "stage", "stage": "review", "provider": agents["reviewer"].provider}
        review = agents["reviewer"].run(
            f"Görev: {task}\n\nÖnerilen değişiklikler (diff):\n{all_diffs}\n\n"
            "Bu değişiklikleri incele; sorun yoksa 'VERDICT: APPROVED', varsa "
            "'VERDICT: NEEDS_FIX' yazıp kısaca açıkla."
        )
        yield track(review, "review")
        yield {"type": "output", "stage": "review", "text": review.text}
        verdict = _parse_verdict(review.text)
        # Kararın kısa gerekçesi (VERDICT satırı hariç ilk anlamlı satır).
        note = ""
        for line in (review.text or "").splitlines():
            s = line.strip()
            if s and not _VERDICT_RE.search(s):
                note = s[:200]
                break
        yield {"type": "verdict", "verdict": verdict or "UNKNOWN", "note": note}

    totals["cost_usd"] = round(totals["cost_usd"], 5)
    totals["latency_s"] = round(totals["latency_s"], 2)
    # 6) Öneriyi tek pakette gönder — arayüz onay için saklar.
    yield {"type": "proposal", "proposals": proposals, "totals": totals, "verdict": verdict}
