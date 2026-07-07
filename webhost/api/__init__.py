"""webhost.api — köprü domain handler'ları. Her modül import edildiğinde
@handler dekoratörleriyle kendini kaydeder; register_all() hepsini yükler."""


def register_all() -> None:
    from webhost.api import app as _app          # noqa: F401
    from webhost.api import settings as _settings  # noqa: F401
    from webhost.api import project as _project    # noqa: F401
    from webhost.api import fs as _fs              # noqa: F401
    from webhost.api import session as _session    # noqa: F401
