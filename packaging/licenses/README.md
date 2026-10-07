# Version-specific runtime license payloads

`node-v24.19.0-LICENSE.txt` is the complete Node.js runtime/third-party license
text fetched over HTTPS from the matching official source tag:

- https://raw.githubusercontent.com/nodejs/node/v24.19.0/LICENSE
- SHA-256: `148eacf7863ef4329224a29398623077200a27194aa075569faf4a0a85566ca5`

The nodejs-wheel Python distribution's own MIT license does **not** replace the
embedded runtime's license/third-party notices. The PyInstaller spec ships this
file under `_internal/licenses/`. It looks up the installed nodejs-wheel-binaries
version and refuses packaging if the matching vendored license is absent; it
never downloads one automatically. When updating Node, vendor the official
matching license and review redistribution obligations before building.

Additional offline payloads:

- `LGPL-3.0.txt`: https://www.gnu.org/licenses/lgpl-3.0.txt;
  SHA-256 `e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118`.
- `GPL-3.0.txt`: https://www.gnu.org/licenses/gpl-3.0.txt;
  SHA-256 `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986`.
- `react-remove-scroll-bar-2.3.8-LICENSE.txt`: installed package lacks its text;
  retrieved from upstream `LICENSE`, commit
  `7301c160fda44cb8cf2b9fdfde61efad35736196` at
  https://github.com/theKashey/react-remove-scroll-bar.
  SHA-256 `a79aae0c0f21990d9d963bb3c5a79cdcea9a46f8523ba55c58d7fe776b6ebc84`.
  Version-tag URLs were unavailable; this is license-source provenance, not a
  claim of a verified version-tag source snapshot. Fallback is only for 2.3.8.

PyInstaller bootloader license/exception is copied from the actual build tool's
installed `COPYING.txt`. `requirements-build.txt` pins the Node version matching
its vendored text. GNU texts do not classify every Qt add-on as LGPL. The custom
Widget/WebEngine hooks now exclude unused GPL-only QML/PDF/VirtualKeyboard/Quick3D
payloads, with a fail-closed bundle scope check. Exact Windows binaries and
Qt/Chromium third-party/source redistribution obligations remain publication
gates rather than claims of automatic legal certification.

Runtime Python distribution license metadata is separately collected. This
payload and the offline inventory are not a comprehensive legal/compliance
certification of all bundled libraries, fonts, frontend or native system files.
