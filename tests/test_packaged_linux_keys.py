"""Linux frozen plaintext credential fallback: private atomic files, no DPAPI."""
import os
import sys

import pytest

from webhost.api import keys


@pytest.fixture
def frozen_linux(monkeypatch):
    if os.name != 'posix':
        pytest.skip('POSIX permission contracts')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'linux')


def test_frozen_linux_save_is_owner_only_and_preserves_metadata(tmp_path, frozen_linux):
    target = tmp_path / '.env'
    target.write_text('# retain\nMODEL=local\nAPI_KEY=old\n')
    target.chmod(0o644)
    keys.write_env(target, {'API_KEY': 'fixture-value'})
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.read_text() == '# retain\nMODEL=local\nAPI_KEY=fixture-value\n'
    assert list(tmp_path.iterdir()) == [target]
    keys.clear_env_keys(target, {'API_KEY'})
    assert target.stat().st_mode & 0o777 == 0o600
    assert 'API_KEY=\n' in target.read_text()


def test_frozen_linux_refuses_symlink_without_changing_target(tmp_path, frozen_linux):
    outside = tmp_path / 'outside'
    outside.write_text('original')
    link = tmp_path / '.env'
    link.symlink_to(outside)
    with pytest.raises(OSError, match='symlink'):
        keys.write_env(link, {'API_KEY': 'fixture-value'})
    assert outside.read_text() == 'original'


def test_failed_atomic_save_keeps_old_credentials(tmp_path, frozen_linux, monkeypatch):
    target = tmp_path / '.env'
    target.write_text('API_KEY=old\n')
    def denied(*args):
        raise OSError('fixture replace failed')
    monkeypatch.setattr(os, 'replace', denied)
    with pytest.raises(OSError):
        keys.write_env(target, {'API_KEY': 'new'})
    assert target.read_text() == 'API_KEY=old\n'
    assert list(tmp_path.iterdir()) == [target]
