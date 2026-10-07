"""GUI-only compatibility flags; helper dispatch remains untouched."""
import sys

import pytest

import shell


@pytest.mark.parametrize('frozen,platform,args,software', [
    (True, 'linux', [], True),
    (True, 'linux', ['--hardware-rendering'], False),
    (False, 'linux', [], False),
    (True, 'win32', [], False),
    (False, 'linux', ['--software-rendering'], True),
])
def test_graphics_defaults_are_platform_and_mode_bounded(monkeypatch, frozen, platform, args, software):
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    monkeypatch.setattr(sys, 'platform', platform)
    monkeypatch.setattr(sys, 'argv', ['ImeceIDE', *args])
    monkeypatch.setenv('QTWEBENGINE_CHROMIUM_FLAGS', '--lang=en')
    shell._configure_graphics()
    import os
    flags = os.environ['QTWEBENGINE_CHROMIUM_FLAGS']
    assert ('--disable-gpu' in flags) is software
    assert '--lang=en' in flags
    assert '--no-sandbox' not in flags
    shell._configure_graphics()
    assert os.environ['QTWEBENGINE_CHROMIUM_FLAGS'].count('--disable-gpu') <= 1


def test_mutually_exclusive_graphics_options_are_refused(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['ImeceIDE', '--software-rendering', '--hardware-rendering'])
    with pytest.raises(ValueError, match='either'):
        shell._configure_graphics()


def test_helpers_return_before_graphics_configuration(monkeypatch):
    monkeypatch.setattr(shell, '_run_packaged_helper', lambda: 125)
    def unexpected():
        pytest.fail('Helpers must not configure GUI graphics')
    monkeypatch.setattr(shell, '_configure_graphics', unexpected)
    assert shell.main() == 125
