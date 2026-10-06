from pathlib import Path
import re

import pytest

from tools.check_repo_hygiene import check


def _fixture(tmp_path: Path, markdown: str, files: dict[str, str] | None = None):
    tracked = {"docs/page.md"}
    for name, content in (files or {}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        tracked.add(name)
    doc = tmp_path / "docs/page.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(markdown, encoding="utf-8")
    return check(tmp_path, tracked)


def test_relative_image_and_reference_links_resolve(tmp_path):
    errors = _fixture(
        tmp_path,
        """[guide](../guide.md#install)
![diagram](images/diagram.png "overview")
[reference]: ../reference%20notes.md "notes"
[jump](#section)
""",
        {
            "guide.md": "guide",
            "docs/images/diagram.png": "image",
            "reference notes.md": "reference",
        },
    )
    assert errors == []


def test_fenced_code_examples_are_ignored(tmp_path):
    errors = _fixture(
        tmp_path,
        """Example:
```md
[missing](missing.md)
![missing](no-image.png)
```
~~~
[also missing](not-here.md)
~~~
""",
    )
    assert errors == []


def test_reports_missing_untracked_and_repository_escape_targets(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/untracked.md").write_text("untracked", encoding="utf-8")
    errors = _fixture(
        tmp_path,
        """[missing](absent.md)
[untracked](untracked.md)
[escape](../../outside.md)
""",
    )
    assert len(errors) == 3
    assert "docs/page.md:1" in errors[0] and "missing or not tracked" in errors[0]
    assert "docs/page.md:2" in errors[1] and "not tracked" in errors[1]
    assert "docs/page.md:3" in errors[2] and "escapes the repository" in errors[2]


def test_external_mailto_and_nested_fence_examples_are_ignored(tmp_path):
    errors = _fixture(
        tmp_path,
        """[web](https://example.invalid/path)
[email](mailto:someone@example.invalid)
```markdown
~~~
[still code](missing.md)
~~~
```
""",
    )
    assert errors == []


def test_malformed_local_url_reports_source_line(tmp_path):
    errors = _fixture(tmp_path, "[bad](http://[)\n")
    assert len(errors) == 1
    assert "docs/page.md:1" in errors[0]
    assert "malformed URL syntax" in errors[0]


def test_directory_links_require_tracked_descendants(tmp_path):
    (tmp_path / "docs/tracked").mkdir(parents=True)
    (tmp_path / "docs/tracked/file.md").write_text("tracked", encoding="utf-8")
    (tmp_path / "docs/untracked").mkdir()
    (tmp_path / "docs/untracked/loose.md").write_text("untracked", encoding="utf-8")
    doc = tmp_path / "docs/page.md"
    doc.write_text("[tracked](tracked/)\n[untracked](untracked/)\n", encoding="utf-8")
    errors = check(tmp_path, {"docs/page.md", "docs/tracked/file.md"})
    assert len(errors) == 1
    assert "docs/page.md:2" in errors[0]
    assert "has no tracked files" in errors[0]


def test_markdown_source_symlink_cannot_escape_repository(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("[bad](missing.md)\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    source = docs / "linked.md"
    try:
        source.symlink_to(outside)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symlinks unavailable: {error}")
    errors = check(tmp_path, {"docs/linked.md"})
    assert errors == ["docs/linked.md: tracked Markdown source resolves outside the repository"]


def test_missing_tracked_markdown_source_is_reported(tmp_path):
    assert check(tmp_path, {"docs/missing.md"}) == [
        "docs/missing.md: tracked Markdown source is missing or is not a file"
    ]


def test_release_tag_pattern_is_anchored_and_whitespace_safe():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
    text = workflow.read_text(encoding="utf-8")
    match = re.search(r"\$pattern = '([^']+)'", text)
    assert match is not None
    assert "[regex]::Match($env:RELEASE_TAG, $pattern" in text
    assert "$tagMatch.Groups[4].Value" in text
    assert "StartsWith('0')" in text
    assert match.group(1).startswith(r"\A") and match.group(1).endswith(r"\z")
    assert "ref: refs/tags/${{ steps.validate.outputs.tag }}" in text
    assert 'EscapeDataString("refs/tags/$env:RELEASE_TAG")' in text
    assert "--verify-tag" in text

    # Structural regression check only: adapt .NET's \z to Python's equivalent;
    # this does not execute or validate PowerShell itself.
    pattern = re.compile(match.group(1).replace(r"\z", r"\Z"))

    def accepted(tag: str) -> bool:
        result = pattern.fullmatch(tag)
        if result is None:
            return False
        return not any(
            identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
            for identifier in (result.group(4) or "").split(".")
        )

    assert all(accepted(tag) for tag in ["v0.4.0", "v1.2.3-beta.1", "v1.2.3+build.4"])
    assert not any(
        accepted(tag)
        for tag in [
            "v1.2.3\n",
            "v1.2.3 ",
            " v1.2.3",
            "v1.2.3;gh",
            "v01.2.3",
            "v1.2.3-beta.01",
        ]
    )
