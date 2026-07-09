"""project_runner plan ayrıştırma sözleşmesi."""

from project_runner import _parse_requested_files, _plan_summary


def test_plan_summary_strips_files_protocol_block():
    text = "1. Dosyayı incele\n2. Dönüşümü yap\n\nFILES:\n- src/a.py\n- src/b.py"
    assert _plan_summary(text) == "1. Dosyayı incele\n2. Dönüşümü yap"


def test_requested_files_only_allows_project_files_and_caps_at_eight():
    valid = {f"src/{n}.py" for n in range(10)}
    text = "FILES:\n" + "\n".join(f"- src/{n}.py" for n in range(10)) + "\n- dışarı.py"
    assert _parse_requested_files(text, valid) == [f"src/{n}.py" for n in range(8)]
