"""webhost.api — köprü domain handler'ları. Her modül import edildiğinde
@handler dekoratörleriyle kendini kaydeder; register_all() hepsini yükler."""


def register_all() -> None:
    from webhost.api import app as _app          # noqa: F401
    from webhost.api import settings as _settings  # noqa: F401
    from webhost.api import project as _project    # noqa: F401
    from webhost.api import fs as _fs              # noqa: F401
    from webhost.api import session as _session    # noqa: F401
    from webhost.api import run as _run            # noqa: F401
    from webhost.api import history as _history    # noqa: F401
    from webhost.api import terminal as _terminal  # noqa: F401
    from webhost.api import search as _search      # noqa: F401
    from webhost.api import scm as _scm            # noqa: F401
    from webhost.api import lsp as _lsp            # noqa: F401
    from webhost.api import exec as _exec          # noqa: F401
