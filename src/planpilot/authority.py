"""Atomic runtime bridge over the audited PlanStore and ApprovalService.

The bridge owns no plan or approval policy.  It clones both validated service
states, invokes their public methods, and commits their canonical snapshots to
SQLite together.  A failed operation leaves both durable and live state intact.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import tempfile

from .approval import ApprovalService
from .clock import Clock, WallClock
from .persistence import Database
from .store import PlanNotFoundError, PlanStore


ROLE_LABELS = {
    "planner": "Production Planner",
    "manager": "Production Manager",
}


class AuthorityConflictError(RuntimeError):
    """The durable snapshot changed since this bridge loaded it."""


class _Unchanged:
    def __init__(self, value):
        self.value = value


class RuntimeAuthority:
    """The only runtime write path for plans, approvals and publication."""

    def __init__(self, database: Database, clock: Clock | None = None):
        self.database = database
        # Every authoritative timestamp comes from this server-owned clock.
        # Callers never supply one; see planpilot.clock for the boundary rule.
        self.clock = clock or WallClock()
        with self.database.lock:
            row = self.database.conn.execute(
                "SELECT revision,plan_store_json,approval_service_json "
                "FROM authority_state WHERE singleton=1"
            ).fetchone()
        if row is None:
            store = PlanStore()
            approvals = ApprovalService(store)
            store_text, approval_text = self._serialize(store, approvals)
            with self.database.transaction():
                self.database.conn.execute(
                    "INSERT OR IGNORE INTO authority_state VALUES(1,0,?,?,?)",
                    (store_text, approval_text, self.clock.now()),
                )
            with self.database.lock:
                row = self.database.conn.execute(
                    "SELECT revision,plan_store_json,approval_service_json "
                    "FROM authority_state WHERE singleton=1"
                ).fetchone()
        self._revision = row["revision"]
        self._store, self._approvals = self._load_pair(
            row["plan_store_json"], row["approval_service_json"]
        )

    @staticmethod
    @contextmanager
    def _workspace():
        with tempfile.TemporaryDirectory(prefix="planpilot-authority-") as directory:
            yield Path(directory)

    @classmethod
    def _serialize(cls, store: PlanStore, approvals: ApprovalService) -> tuple[str, str]:
        with cls._workspace() as directory:
            store_text = store.dump_state(directory / "store.json")
            approval_text = approvals.dump_state(directory / "approvals.json").rstrip("\n")
        return store_text, approval_text

    @classmethod
    def _load_pair(cls, store_text: str, approval_text: str):
        with cls._workspace() as directory:
            store_path = directory / "store.json"
            approval_path = directory / "approvals.json"
            store_path.write_text(store_text + "\n", encoding="utf-8")
            approval_path.write_text(approval_text.rstrip("\n") + "\n", encoding="utf-8")
            store = PlanStore()
            store.load_state(store_path)
            approvals = ApprovalService(store)
            approvals.load_state(approval_path)
        return store, approvals

    def _clone(self):
        store_text, approval_text = self._serialize(self._store, self._approvals)
        return self._load_pair(store_text, approval_text)

    def _mutate(self, operation, *, plan_id=None, actor="system", event="authority_update"):
        store, approvals = self._clone()
        result = operation(store, approvals)
        if isinstance(result, _Unchanged):
            with self.database.lock:
                durable = self.database.conn.execute(
                    "SELECT revision FROM authority_state WHERE singleton=1"
                ).fetchone()[0]
            if durable != self._revision:
                raise AuthorityConflictError("authority snapshot changed; reload required")
            return result.value
        store_text, approval_text = self._serialize(store, approvals)
        next_revision = self._revision + 1
        with self.database.transaction():
            cursor = self.database.conn.execute(
                "UPDATE authority_state SET revision=?,plan_store_json=?,"
                "approval_service_json=?,updated_at=? "
                "WHERE singleton=1 AND revision=?",
                (next_revision, store_text, approval_text, self.clock.now(), self._revision),
            )
            if cursor.rowcount != 1:
                raise AuthorityConflictError("authority snapshot changed; reload required")
            self.database._audit(plan_id, actor, event, {"revision": next_revision})
        self._store, self._approvals, self._revision = store, approvals, next_revision
        return result

    def install_validated_plan(
        self,
        content: dict,
        validation_result: dict,
        impact_by_action: dict[str, dict],
        horizon_end: str,
        validated_at: str,
        actor: str = "system",
    ) -> dict:
        """Atomically store schema/digest-valid content and validator evidence."""
        if not validation_result.get("digest_verified"):
            raise ValueError("validator must independently verify the plan digest")
        if not validation_result.get("is_feasible"):
            raise ValueError("only independently feasible plans may enter authority")
        if validation_result.get("hard_violations"):
            raise ValueError("hard-constraint violations prevent authoritative storage")
        binding = (content.get("plan_id"), content.get("plan_version"), content.get("plan_digest"))
        if binding != (
            validation_result.get("plan_id"),
            validation_result.get("plan_version"),
            validation_result.get("plan_digest"),
        ):
            raise ValueError("validator evidence does not bind the supplied plan content")
        if validation_result.get("recomputed_plan_digest") != content.get("plan_digest"):
            raise ValueError("validator recomputed digest does not match plan content")

        def operation(store, approvals):
            if store.has(content["plan_id"]):
                lifecycle = store.commit_new_version(content, validated_at)
                for event in store.pending_superseded():
                    if event["approval_set_id"] is not None:
                        approvals.invalidate_set(
                            event["approval_set_id"], event["invalidation_cause"],
                            event["plan_digest"],
                        )
                    store.acknowledge_superseded(
                        event["plan_id"], event["plan_version"],
                        event["plan_digest"], event["approval_set_id"], validated_at,
                    )
            else:
                if content["plan_version"] != 1:
                    raise ValueError("the first authoritative plan version must be 1")
                digest = store.put_content(content)
                lifecycle = store.create_lifecycle(
                    content["plan_id"], content["plan_version"], digest, validated_at
                )
            store.transition(
                content["plan_id"], content["plan_version"], "PROPOSED",
                validated_at, expected_plan_version=content["plan_version"],
            )
            approvals.record_validated_plan(
                validation_result, impact_by_action, horizon_end, validated_at
            )
            return {
                "content": store.get_content(content["plan_id"], content["plan_version"]),
                "lifecycle": store.get_lifecycle(content["plan_id"], content["plan_version"]),
            }

        return self._mutate(
            operation, plan_id=content.get("plan_id"), actor=actor,
            event="validated_plan_installed",
        )

    def install_generated_plan(
        self, content: dict, validator, impact_builder,
        horizon_end: str, validated_at: str, actor: str = "system",
    ) -> dict:
        """Stage, reload and independently validate content in one transaction.

        The validator receives the immutable record reloaded from the cloned
        PlanStore. Any schema, digest, KPI or semantic failure aborts before the
        cloned Store/Approval state becomes durable.
        """
        def operation(store, approvals):
            if store.has(content["plan_id"]):
                store.commit_new_version(content, validated_at)
                for event in store.pending_superseded():
                    if event["approval_set_id"] is not None:
                        approvals.invalidate_set(
                            event["approval_set_id"], event["invalidation_cause"],
                            event["plan_digest"],
                        )
                    store.acknowledge_superseded(
                        event["plan_id"], event["plan_version"],
                        event["plan_digest"], event["approval_set_id"], validated_at,
                    )
            else:
                if content["plan_version"] != 1:
                    raise ValueError("the first authoritative plan version must be 1")
                digest = store.put_content(content)
                store.create_lifecycle(
                    content["plan_id"], content["plan_version"], digest, validated_at
                )

            stored = store.get_content(content["plan_id"], content["plan_version"])
            validation_result = validator(stored)
            if not validation_result.get("digest_verified"):
                raise ValueError("validator must independently verify the plan digest")
            if not validation_result.get("is_feasible"):
                raise ValueError(validation_result.get("infeasible_reason") or "independent validation rejected plan")
            if validation_result.get("hard_violations"):
                raise ValueError("hard-constraint violations prevent authoritative storage")
            binding = (stored["plan_id"], stored["plan_version"], stored["plan_digest"])
            if binding != (
                validation_result.get("plan_id"), validation_result.get("plan_version"),
                validation_result.get("plan_digest"),
            ):
                raise ValueError("validator evidence does not bind the stored plan content")
            if validation_result.get("recomputed_plan_digest") != stored["plan_digest"]:
                raise ValueError("validator recomputed digest does not match stored plan content")

            store.transition(
                stored["plan_id"], stored["plan_version"], "PROPOSED",
                validated_at, expected_plan_version=stored["plan_version"],
            )
            approvals.record_validated_plan(
                validation_result, impact_builder(validation_result),
                horizon_end, validated_at,
            )
            return {
                "content": stored,
                "lifecycle": store.get_lifecycle(stored["plan_id"], stored["plan_version"]),
                "validation_result": validation_result,
            }

        return self._mutate(
            operation, plan_id=content.get("plan_id"), actor=actor,
            event="generated_plan_validated_and_installed",
        )

    def get_plan(self, plan_id: str, version: int | None = None) -> dict | None:
        try:
            content = self._store.get_content(plan_id, version)
            lifecycle = self._store.get_lifecycle(plan_id, content["plan_version"])
        except PlanNotFoundError:
            return None
        self._store.verify_digest(plan_id, content["plan_version"])
        return {"content": content, "lifecycle": lifecycle}

    def next_version(self, plan_id: str) -> int:
        """Return the next immutable version for one server-owned plan id."""
        current = self.get_plan(plan_id)
        return 1 if current is None else current["content"]["plan_version"] + 1

    def request_approval(
        self, plan_id: str, version: int, digest: str,
        action: str, actor: str, expires_at: str | None = None,
    ) -> dict:
        # `expires_at` may be caller-PROPOSED per the contract; the server
        # clamps it against clock.now(). No caller may supply server_now.
        timestamp = self.clock.now()

        def operation(store, approvals):
            snapshot = approvals.request_approval(
                plan_id, version, digest, action, timestamp, expires_at
            )
            store.transition(
                plan_id, version, "AWAITING_APPROVAL", timestamp,
                approval_set_id=snapshot["approval_set_id"],
                expected_plan_version=version,
            )
            return snapshot

        return self._mutate(
            operation, plan_id=plan_id, actor=actor, event="approval_requested"
        )

    def required_permission(self, approval_request_id: str) -> str:
        requirement = self._approvals.requirement_for_request(approval_request_id)
        return {
            "publish_plan": "approve_publish",
            "assign_qualified_secondary_skill": "approve_secondary",
            "add_overtime": "approve_overtime",
            "change_promised_due_date": "approve_due_date",
        }[requirement["action"]]

    def check_approval_status(
        self, approval_set_id: str, plan_id: str, version: int, digest: str,
    ) -> dict:
        timestamp = self.clock.now()
        return self._mutate(
            lambda _store, approvals: approvals.check_approval_status(
                approval_set_id, plan_id, version, digest, timestamp
            ),
            plan_id=plan_id, event="approval_status_checked",
        )

    def decide_approval(
        self, approval_request_id: str, decision: str, actor: str,
        application_role: str,
        decision_reason: str | None = None, decision_comment: str | None = None,
    ) -> dict:
        # decided_at is server-owned: never a parameter a caller can set.
        timestamp = self.clock.now()
        role = ROLE_LABELS.get(application_role)
        if role is None:
            raise PermissionError("unknown authenticated role")

        def operation(store, approvals):
            snapshot = approvals.record_decision(
                approval_request_id, decision, role, actor, timestamp,
                decision_reason, decision_comment,
            )
            if snapshot["aggregate_status"] == "APPROVED":
                lifecycle = store.get_lifecycle(snapshot["plan_id"], snapshot["plan_version"])
                if lifecycle["status"] != "APPROVED":
                    store.transition(
                        snapshot["plan_id"], snapshot["plan_version"], "APPROVED",
                        timestamp, approval_set_id=snapshot["approval_set_id"],
                        expected_plan_version=snapshot["plan_version"],
                    )
            return snapshot

        return self._mutate(
            operation, actor=actor, event="approval_decided"
        )

    def publish_plan(
        self, plan_id: str, version: int, digest: str,
        approval_set_id: str, actor: str, application_role: str,
    ) -> dict:
        if application_role != "planner":
            raise PermissionError("only Production Planner may publish")
        timestamp = self.clock.now()

        def operation(store, approvals):
            content = store.get_content(plan_id, version)
            recomputed = store.verify_digest(plan_id, version)
            if digest != recomputed or content["plan_digest"] != recomputed:
                raise ValueError("published plan digest does not match stored content")
            lifecycle = store.get_lifecycle(plan_id, version)
            if lifecycle["status"] == "PUBLISHED":
                if lifecycle["approval_set_id"] != approval_set_id:
                    raise ValueError("publication retry uses a different approval set")
                return _Unchanged(lifecycle)
            approvals.require_approved(
                approval_set_id, plan_id, version, digest, timestamp
            )
            if lifecycle["status"] != "APPROVED":
                store.transition(
                    plan_id, version, "APPROVED", timestamp,
                    approval_set_id=approval_set_id,
                    expected_plan_version=version,
                )
            return store.transition(
                plan_id, version, "PUBLISHED", timestamp,
                approval_set_id=approval_set_id,
                published_version=version,
                expected_plan_version=version,
            )

        return self._mutate(
            operation, plan_id=plan_id, actor=actor, event="plan_published"
        )

    @property
    def revision(self) -> int:
        return self._revision
