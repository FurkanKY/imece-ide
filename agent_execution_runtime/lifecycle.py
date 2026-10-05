"""Role-free canonical lifecycle for single-agent runs."""

from run_runtime.events import RunEventType
from run_runtime.legacy import LIFECYCLE_SOURCE, LegacyRunCoordinator
from run_runtime.service import RunRuntime


class AgentRunCoordinator(LegacyRunCoordinator):
    """Legacy event/settlement interface with single-provider start metadata."""

    @classmethod
    def start(
        cls, runtime: RunRuntime, *, project_root: str, task: str, provider_id: str,
    ) -> "AgentRunCoordinator":
        task_record = runtime.create_task(project_root=project_root, prompt=task)
        run_record = runtime.create_run(
            task_id=task_record.task_id, routing={"agent_provider": provider_id},
        )
        coordinator = cls(
            runtime, task_id=task_record.task_id, run_id=run_record.run_id,
            routing=dict(run_record.routing),
        )
        try:
            runtime.record(
                run_id=coordinator.run_id, type=RunEventType.RUN_CREATED, payload={},
                source=LIFECYCLE_SOURCE, correlation_id=coordinator.run_id,
            )
            runtime.record(
                run_id=coordinator.run_id, type=RunEventType.RUN_STARTED, payload={},
                source=LIFECYCLE_SOURCE, correlation_id=coordinator.run_id,
            )
        except Exception:
            try:
                runtime.record(
                    run_id=coordinator.run_id, type=RunEventType.RUN_FAILED,
                    payload={
                        "error_code": "legacy_lifecycle_start_failed",
                        "error_message": "Run yaşam döngüsü başlatma (run.created/run.started) başarısız.",
                    },
                    source=LIFECYCLE_SOURCE, correlation_id=coordinator.run_id,
                )
            except Exception:
                pass
            raise
        return coordinator
