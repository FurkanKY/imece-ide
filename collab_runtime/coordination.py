"""Bounded, conservative projections of published coordination state."""

from __future__ import annotations

from typing import Any

from collab_runtime.errors import ValidationError
from collab_runtime.models import ACTIVE_STATUSES, SessionState, parse_state_dict, sha_hex

MAX_EVENT_TASKS = 32
MAX_AFFECTED_TASKS = 32


def project_change(previous: SessionState, current: SessionState,
                   from_revision: str, to_revision: str) -> dict[str, Any]:
    """Return safe metadata describing one adjacent pair of strict states."""
    try:
        from_revision = sha_hex(from_revision, "from_revision")
        to_revision = sha_hex(to_revision, "to_revision")
        old = parse_state_dict(previous.to_dict())
        new = parse_state_dict(current.to_dict())
    except Exception:
        raise ValidationError("invalid change projection input") from None
    identity = (old.session_id, old.base_commit, old.target_version)
    if identity != (new.session_id, new.base_commit, new.target_version):
        raise ValidationError("change projection identity mismatch")
    if from_revision == to_revision and old.to_dict() != new.to_dict():
        raise ValidationError("same revision has different state")

    old_interfaces, new_interfaces = dict(old.context.interfaces), dict(new.context.interfaces)
    interface_ids = sorted(set(old_interfaces) | set(new_interfaces))
    added_interfaces = [key for key in interface_ids if key not in old_interfaces]
    removed_interfaces = [key for key in interface_ids if key not in new_interfaces]
    changed_interfaces = [key for key in interface_ids if key in old_interfaces and key in new_interfaces and old_interfaces[key] != new_interfaces[key]]
    context_changed = old.context.to_dict() != new.context.to_dict()
    changed_ids = sorted(key for key in set(old.tasks) | set(new.tasks)
                         if key not in old.tasks or key not in new.tasks or old.tasks[key] != new.tasks[key])
    records = []
    for task_id in changed_ids[:MAX_EVENT_TASKS]:
        before, after = old.tasks.get(task_id), new.tasks.get(task_id)
        records.append({"taskId": task_id, "change": "added" if before is None else "removed" if after is None else "changed",
                        "previousStatus": before.status if before else None, "status": after.status if after else None,
                        "previousOwner": before.owner if before else None, "owner": after.owner if after else None,
                        "fields": ([name for name, differs in (
                            ("goal", before.goal != after.goal), ("assignment", before.owner != after.owner),
                            ("scopes", before.scopes != after.scopes), ("contextRevision", before.context_revision != after.context_revision),
                            ("status", before.status != after.status)) if differs] if before and after else [])})
    affected = sorted(set(changed_ids) | ({task_id for task_id, task in new.tasks.items()
                                          if task.status in ACTIVE_STATUSES} if context_changed else set()))
    return {"fromRevision": from_revision, "toRevision": to_revision,
            "metadataCheckpoint": old.to_dict() == new.to_dict() and from_revision != to_revision,
            "goalChanged": old.context.goal != new.context.goal,
            "decisionsChanged": old.context.decisions != new.context.decisions,
            "interfaces": {"added": added_interfaces, "removed": removed_interfaces, "changed": changed_interfaces},
            "taskChanges": records, "taskChangeCount": len(changed_ids), "taskChangesTruncated": len(changed_ids) > len(records),
            "affectedTaskIds": affected[:MAX_AFFECTED_TASKS], "affectedTaskCount": len(affected),
            "affectedTasksTruncated": len(affected) > MAX_AFFECTED_TASKS,
            "contextChanged": context_changed}
