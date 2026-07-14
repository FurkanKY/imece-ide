# Third-Party Notices

Imece IDE is licensed under [Apache-2.0](LICENSE). It depends on and — in the
packaged Windows distribution — redistributes the following third-party
components. Each component remains under its own license; the license texts
ship with the component packages inside the distribution and are available at
the linked upstream projects.

## Desktop runtime (Python)

| Component | License | Project |
|---|---|---|
| PySide6 / Shiboken6 (Qt for Python) | LGPL-3.0-only | https://www.qt.io/qt-for-python |
| Qt 6 libraries (incl. QtWebEngine, based on Chromium) | LGPL-3.0-only (Chromium parts: BSD-style and others) | https://www.qt.io/ |
| requests | Apache-2.0 | https://github.com/psf/requests |
| python-dotenv | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| Flask | BSD-3-Clause | https://github.com/pallets/flask |
| pywinpty | MIT | https://github.com/andfoy/pywinpty |
| basedpyright (includes Pyright, © Microsoft) | MIT | https://github.com/DetachHead/basedpyright |
| debugpy | MIT | https://github.com/microsoft/debugpy |
| nodejs-wheel-binaries (bundles the Node.js runtime) | MIT (Node.js: MIT and third-party licenses) | https://github.com/njzjz/nodejs-wheel |

## Frontend (bundled into the UI build)

| Component | License | Project |
|---|---|---|
| React / React DOM | MIT | https://react.dev/ |
| monaco-editor (© Microsoft) | MIT | https://github.com/microsoft/monaco-editor |
| @xterm/xterm and addons | MIT | https://github.com/xtermjs/xterm.js |
| zustand | MIT | https://github.com/pmndrs/zustand |
| motion | MIT | https://github.com/motiondivision/motion |
| radix-ui | MIT | https://github.com/radix-ui/primitives |
| lucide-react | ISC | https://github.com/lucide-icons/lucide |
| react-markdown | MIT | https://github.com/remarkjs/react-markdown |
| remark-gfm | MIT | https://github.com/remarkjs/remark-gfm |
| Tailwind CSS | MIT | https://github.com/tailwindlabs/tailwindcss |

## Fonts

| Component | License | Project |
|---|---|---|
| JetBrains Mono | SIL OFL 1.1 ([full text](web/ui/public/fonts/JetBrainsMono-OFL.txt)) | https://github.com/JetBrains/JetBrainsMono |

## Build-time tools (not redistributed)

Vite (MIT), TypeScript (Apache-2.0), Playwright (Apache-2.0) and
PyInstaller (GPL-2.0-or-later with the Bootloader Exception, which permits
distributing packaged applications under their own license) are used only to
build and test Imece IDE and are not part of the shipped application logic.

## LGPL notice for the packaged Windows build

The packaged Windows distribution includes the Qt 6 / PySide6 libraries under
LGPL-3.0. The package uses PyInstaller's `onedir` layout: the Qt shared
libraries are separate DLL files inside the distribution folder, so users can
replace them with modified or newer compatible versions. Qt source code is
available at https://download.qt.io/ and https://code.qt.io/. The LGPL-3.0
license text is available at https://www.gnu.org/licenses/lgpl-3.0.html and
ships inside the PySide6 package metadata included in the distribution.
