"""CI-only test partitioning; ordinary local runs retain the complete suite."""
import pytest


def pytest_addoption(parser):
    group = parser.getgroup("imece-ci")
    group.addoption("--ci-shard-index", type=int, default=None)
    group.addoption("--ci-shard-count", type=int, default=1)


def pytest_collection_modifyitems(config, items):
    index = config.getoption("--ci-shard-index")
    count = config.getoption("--ci-shard-count")
    if index is None:
        if count != 1:
            raise pytest.UsageError("--ci-shard-count requires --ci-shard-index")
        return
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError("CI shard requires 0 <= index < count")
    # Every collected item belongs to exactly one shard. File-heavy transport
    # suites no longer place hundreds of temporary Git repositories on one VM.
    selected, deselected = [], []
    for offset, item in enumerate(items):
        (selected if offset % count == index else deselected).append(item)
    if not selected:
        raise pytest.UsageError("empty CI shard")
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
