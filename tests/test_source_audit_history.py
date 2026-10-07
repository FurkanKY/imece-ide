"""Historical synthetic-fixture exemptions require exact reviewed Git evidence."""
import hashlib
from pathlib import Path
import runpy
import subprocess

import pytest

MODULE = runpy.run_path(str(Path(__file__).parents[1] / 'packaging/source_audit.py'))


def git(root, *args):
    return subprocess.check_output(['git', '-c', 'core.hooksPath=', '-c',
                                   'commit.gpgsign=false', '-C', str(root), *args]).decode().strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Fixture Review')
    git(tmp_path, 'config', 'user.email', 'fixture@example.invalid')
    filename = Path('fixture.txt')
    raw = b'reviewed synthetic fixture\n'
    (tmp_path / filename).write_bytes(raw)
    git(tmp_path, 'add', '--', str(filename))
    git(tmp_path, 'commit', '-qm', 'fixture')
    commit = git(tmp_path, 'rev-parse', 'HEAD')
    pin = {'historyCommits': [commit], 'sha256': hashlib.sha256(raw).hexdigest()}
    return tmp_path, filename, commit, pin


def test_explicit_commit_and_exact_blob_are_both_required(repository):
    root, filename, commit, pin = repository
    check = MODULE['reviewed_history_fixture']
    assert check(root, pin, commit, filename)
    assert not check(root, {**pin, 'historyCommits': []}, commit, filename)
    assert not check(root, {**pin, 'sha256': '0' * 64}, commit, filename)
    assert not check(root, pin, '0' * 40, filename)
    assert not check(root, pin, commit, Path('missing.txt'))


@pytest.mark.parametrize('commit', [None, '', '-p', 'HEAD', 'a' * 39, 'z' * 40])
def test_noncanonical_commit_id_cannot_enter_git_argv(repository, commit):
    root, filename, _, pin = repository
    assert not MODULE['reviewed_history_fixture'](root, {**pin, 'historyCommits': [commit]}, commit, filename)


def test_traversal_and_absolute_paths_refused(repository):
    root, _, commit, pin = repository
    for filename in [Path('../fixture.txt'), root / 'fixture.txt']:
        assert not MODULE['reviewed_history_fixture'](root, pin, commit, filename)


def test_git_replace_refs_cannot_forge_reviewed_blob(repository):
    root, filename, commit, pin = repository
    changed = b'different unreviewed evidence\n'
    (root / filename).write_bytes(changed)
    git(root, 'add', '--', str(filename))
    git(root, 'commit', '-qm', 'replacement')
    replacement = git(root, 'rev-parse', 'HEAD')
    git(root, 'replace', commit, replacement)
    check = MODULE['reviewed_history_fixture']
    assert check(root, pin, commit, filename)
    assert not check(root, {**pin, 'sha256': hashlib.sha256(changed).hexdigest()}, commit, filename)
