"""checkpoint.* — Magent apply geri dönüş köprüsü."""

import json

from checkpoints import CheckpointStore
from webhost import state
from webhost.bridge import handler, BridgeError


def _project():
    proj = state.get_project()
    if proj is None:
        raise BridgeError("no_project", "Önce bir proje aç.")
    return proj


@handler("checkpoint.list")
def _list(params, ctx):
    return {"checkpoints": CheckpointStore(_project().root).list()}


@handler("checkpoint.restore")
def _restore(params, ctx):
    proj = _project()
    try:
        restored = CheckpointStore(proj.root).restore(proj, params.get("checkpointId", ""))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        raise BridgeError("checkpoint", str(e))
    if restored:
        ctx._bridge.emit_event("fs.changed", {"kind": "modified", "paths": restored})
    return {"restored": restored}
