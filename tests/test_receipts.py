import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from receipts import ReceiptStore  # noqa: E402


def test_receipt_round_trip_and_markdown_export(tmp_path):
    store = ReceiptStore(str(tmp_path))
    receipt = store.create({
        "task": "Tarihi ISO 8601 yap",
        "plan": {"summary": "Yardımcıyı güncelle", "files": ["src/date.py"]},
        "proposals": [{"path": "src/date.py", "is_new": False, "diff": "-old\n+new"}],
        "review": {"verdict": "APPROVED", "note": "Kapsam doğru."},
    })
    loaded = store.get(receipt["id"])
    assert loaded["task"] == "Tarihi ISO 8601 yap"
    assert loaded["verification"]["status"] == "not_run"
    store.update(receipt["id"], {"status": "applied", "applied": ["src/date.py"], "checkpointId": "abc"})
    output = store.export_markdown(receipt["id"], str(tmp_path))
    text = output.read_text(encoding="utf-8")
    assert "APPROVED" in text
    assert "src/date.py" in text


def test_receipt_rejects_invalid_identifier(tmp_path):
    store = ReceiptStore(str(tmp_path))
    try:
        store.get("../not-a-receipt")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal kabul edilmemeli")
