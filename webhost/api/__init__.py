"""webhost.api — köprü domain handler'ları. Her modül import edildiğinde
@handler dekoratörleriyle kendini kaydeder; register_all() hepsini yükler."""


def register_all() -> None:
    from webhost.api import app as _app          # noqa: F401
    from webhost.api import settings as _settings  # noqa: F401
    from webhost.api import project as _project    # noqa: F401
    from webhost.api import fs as _fs              # noqa: F401
    from webhost.api import session as _session    # noqa: F401
    from webhost.api import run as _run            # noqa: F401
    from webhost.api import checkpoint as _checkpoint  # noqa: F401
    from webhost.api import history as _history    # noqa: F401
    from webhost.api import terminal as _terminal  # noqa: F401
    from webhost.api import search as _search      # noqa: F401
    from webhost.api import scm as _scm            # noqa: F401
    from webhost.api import lsp as _lsp            # noqa: F401
    from webhost.api import exec as _exec          # noqa: F401
    from webhost.api import keys as _keys          # noqa: F401
    from webhost.api import providers as _providers  # noqa: F401
    from webhost.api import debug as _debug        # noqa: F401
    from webhost.api import collab as _collab      # noqa: F401
    from webhost.api import delivery as _delivery  # noqa: F401
    from webhost.api import owner as _owner          # noqa: F401

    # T1.2 — bir önceki oturumun çökmesi/olağandışı kapanması sonrası kalmış
    # olabilecek izole (pipeline) worktree'leri temizle. En iyi çabadır;
    # tek başına register_all()'ı asla başarısız kılmaz.
    try:
        import engine_factory
        engine_factory.prune_startup_workspaces()
    except Exception:
        pass
