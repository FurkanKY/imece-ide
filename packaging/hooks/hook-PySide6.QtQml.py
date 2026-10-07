"""Widget/WebEngine-only Imece shell: preserve Qml ABI, not arbitrary plugins.

QtWebEngine's bindings import QtQuick/QtQml, but our shell creates no QQmlEngine
or QML application. The standard hook copies every installed qmldir plugin,
including unrelated GPL-only Charts/PDF/VirtualKeyboard/Quick3D add-ons. Keep
PyInstaller's native dependency/translation/plugin resolution, omitting only
collect_qtqml_files(). If the shell gains QML, explicitly review/add its imports.
"""
from pathlib import Path
from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
# Native QML debugging is not part of the shell either. Its Quick3D profiler
# alone pulls the otherwise unused Quick3DUtils GPL add-on into the bundle.
binaries = [(source, destination) for source, destination in binaries
            if 'qmldbg_quick3dprofiler' not in Path(source).name.casefold()]
