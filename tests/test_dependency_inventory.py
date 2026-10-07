import importlib.util
import json
from email.message import Message
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[1] / "packaging/dependencies.py"
_spec = importlib.util.spec_from_file_location("imece_dependency_inventory", _SCRIPT)
_inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_inventory)


class Distribution:
    def __init__(self, name, version="1.0", requires=(), **fields):
        self.metadata = Message()
        self.metadata["Name"] = name
        for key, value in fields.items():
            header = key.replace("_", "-")
            for item in value if isinstance(value, list) else [value]:
                self.metadata[header] = item
        self.version = version
        self.requires = list(requires)
        self.files = fields.get("files", [])


def test_runtime_closure_markers_and_requested_extras(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "root[feature]>=1 # comment\n"
        "ignored; python_version < '0'\n",
        encoding="utf-8",
    )
    distributions = [
        Distribution("root", requires=[
            "base>=1",
            "extra-dep; extra == 'feature'",
            "not-on-host; sys_platform == 'never'",
        ]),
        Distribution("base", requires=["leaf; extra == ''"]),
        Distribution("extra-dep"),
        Distribution("leaf"),
        Distribution("unrelated"),
    ]
    report = _inventory.build_inventory(requirements, distributions)
    assert [item["name"] for item in report["packages"]] == ["base", "extra-dep", "leaf", "root"]
    assert report["reviewRequired"] == ["base", "extra-dep", "leaf", "root"]


def test_explicit_optional_seed_and_requirement_version_validation(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("root>=2\n", encoding="utf-8")
    root = Distribution("root", version="2.1")
    sdk = Distribution("typesafe-sdk", version="4.0")
    report = _inventory.build_inventory(requirements, [root, sdk])
    assert {item["name"] for item in report["packages"]} == {"root", "typesafe-sdk"}
    assert {item["name"] for item in _inventory.build_inventory(requirements, [root])["packages"]} == {"root"}
    with pytest.raises(_inventory.InventoryError, match="does not satisfy requirement"):
        _inventory.build_inventory(requirements, [Distribution("root", version="1.0")])


def test_dist_info_root_license_notice_files_are_included(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("pkg\n", encoding="utf-8")
    dist = Distribution("pkg", files=["pkg-1.dist-info/LICENSE.APACHE2", "pkg-1.dist-info/NOTICE", "pkg-1.dist-info/RECORD"])
    report = _inventory.build_inventory(requirements, [dist])
    assert report["packages"][0]["licenseFiles"] == ["pkg-1.dist-info/LICENSE.APACHE2", "pkg-1.dist-info/NOTICE"]


def test_missing_runtime_distribution_fails_clearly(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("absent>=1\n", encoding="utf-8")
    with pytest.raises(_inventory.InventoryError, match="Missing installed distribution: absent"):
        _inventory.build_inventory(requirements, [])


def test_license_metadata_is_deterministic_and_redacted(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("safe\nunknown\n", encoding="utf-8")
    secret_path = "/home/private/.config/credential"
    distributions = [
        Distribution("unknown", License="token=super-secret"),
        Distribution(
            "safe", version="2.0", License_Expression="MIT OR Apache-2.0",
            Classifier=["License :: OSI Approved :: MIT License", "Topic :: Utilities"],
            License_File=["LICENSES/MIT.txt", secret_path, "../escape"],
        ),
    ]
    report = _inventory.build_inventory(requirements, distributions)
    serialized = json.dumps(report, sort_keys=True)
    assert report["reviewRequired"] == ["unknown"]
    assert report["packages"] == [
        {"name": "safe", "version": "2.0", "licenseExpression": "MIT OR Apache-2.0",
         "licenseClassifiers": ["License :: OSI Approved :: MIT License"],
         "licenseFiles": ["LICENSES/MIT.txt"]},
        {"name": "unknown", "version": "1.0", "licenseExpression": None,
         "licenseClassifiers": [], "licenseFiles": []},
    ]
    assert secret_path not in serialized
    assert "super-secret" not in serialized
    assert "unrelated" not in serialized
    assert _inventory.build_inventory(requirements, list(reversed(distributions))) == report
