"""Keep normal Qt GUI plugins except unused GPL-only PDF/virtual keyboard."""
from pathlib import Path
from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
# The shell uses HTML/Monaco and OS keyboard input; it has no Qt PDF image
# renderer or Qt Virtual Keyboard. Filter their initiating plugins before the
# native dependency walker pulls in these unrelated libraries. Keep platform,
# clipboard, ordinary image, accessibility and hardware/software render plugins.
binaries = [(source, destination) for source, destination in binaries
            if not any(token in Path(source).name.casefold()
                       for token in ('qpdf', 'qtvirtualkeyboardplugin'))]
