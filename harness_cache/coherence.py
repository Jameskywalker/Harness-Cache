from __future__ import annotations

from dataclasses import dataclass, field

from .events import EventLog
from .models import (
    CoherenceState,
    HarnessCacheConfig,
    Lease,
    LeaseConflictError,
    LeaseRejectedError,
    LeaseState,
    Pointer,
    utcnow,
)


@dataclass
class HolderDirectoryEntry:
    pointer_version: int
    holders: set[str] = field(default_factory=set)
    state: CoherenceState = CoherenceState.SHARED


class CoherenceManager:
    def __init__(self, event_log: EventLog, config: HarnessCacheConfig) -> None:
        self.event_log = event_log
        self.config = config
        self.holder_directory: dict[str, HolderDirectoryEntry] = {}
        self.leases: dict[str, Lease] = {}

    def register_holder(self, agent_id: str, pointer: Pointer) -> None:
        entry = self.holder_directory.setdefault(
            pointer.pointer_id,
            HolderDirectoryEntry(pointer_version=pointer.pointer_version, state=pointer.coherence_state),
        )
        entry.pointer_version = max(entry.pointer_version, pointer.pointer_version)
        entry.state = pointer.coherence_state
        entry.holders.add(agent_id)

    def remove_holder(self, agent_id: str, pointer_id: str) -> None:
        entry = self.holder_directory.get(pointer_id)
        if entry is not None:
            entry.holders.discard(agent_id)

    def acquire_lease(
        self,
        agent_id: str,
        pointer: Pointer,
        purpose: str,
        ttl_ms: int | None = None,
    ) -> Lease:
        self._expire_old_leases()
        active = self._active_lease_for(pointer.pointer_id)
        if active is not None and active.holder_agent != agent_id:
            raise LeaseConflictError(f"pointer {pointer.pointer_id} already leased by {active.holder_agent}")
        if pointer.coherence_state == CoherenceState.INVALID:
            raise LeaseRejectedError("invalid pointers cannot be edited without rediscovery")
        lease = Lease.grant(
            pointer_id=pointer.pointer_id,
            holder_agent=agent_id,
            purpose=purpose,
            ttl_ms=ttl_ms if ttl_ms is not None else self.config.lease_ttl_seconds * 1000,
        )
        self.leases[lease.lease_id] = lease
        entry = self.holder_directory.setdefault(
            pointer.pointer_id,
            HolderDirectoryEntry(pointer_version=pointer.pointer_version),
        )
        entry.holders.add(agent_id)
        entry.state = CoherenceState.EXCLUSIVE
        self.event_log.append(
            "LEASE_ACQUIRED",
            lease_id=lease.lease_id,
            pointer_id=pointer.pointer_id,
            holder_agent=agent_id,
            purpose=purpose,
            expires_at=lease.expires_at.isoformat(),
        )
        return lease

    def require_active_lease(self, lease_id: str, agent_id: str, pointer_id: str) -> Lease:
        lease = self.leases.get(lease_id)
        if lease is None:
            raise LeaseRejectedError("unknown lease")
        if not lease.is_active():
            lease.lease_state = LeaseState.EXPIRED
            raise LeaseRejectedError("lease expired")
        if lease.holder_agent != agent_id or lease.pointer_id != pointer_id:
            raise LeaseRejectedError("lease does not match agent or pointer")
        return lease

    def commit_lease(self, lease_id: str) -> None:
        lease = self.leases[lease_id]
        lease.lease_state = LeaseState.COMMITTED

    def invalidate_holders(
        self,
        pointer: Pointer,
        old_pointer_version: int,
        new_pointer_version: int,
        reason: str,
        exclude_agent: str | None = None,
    ) -> tuple[str, ...]:
        entry = self.holder_directory.setdefault(
            pointer.pointer_id,
            HolderDirectoryEntry(pointer_version=old_pointer_version),
        )
        targets = tuple(sorted(holder for holder in entry.holders if holder != exclude_agent))
        entry.pointer_version = new_pointer_version
        entry.state = pointer.coherence_state
        return targets

    def append_invalidation(
        self,
        pointer: Pointer,
        old_pointer_version: int,
        reason: str,
        target_agents: tuple[str, ...],
    ):
        return self.event_log.append(
            "POINTER_INVALIDATE",
            pointer_id=pointer.pointer_id,
            old_pointer_version=old_pointer_version,
            new_pointer_version=pointer.pointer_version,
            source_id=pointer.source_id,
            source_version=pointer.source_version,
            reason=reason,
            target_agents=target_agents,
        )

    def _active_lease_for(self, pointer_id: str) -> Lease | None:
        for lease in self.leases.values():
            if lease.pointer_id == pointer_id and lease.is_active():
                return lease
        return None

    def _expire_old_leases(self) -> None:
        now = utcnow()
        for lease in self.leases.values():
            if lease.lease_state == LeaseState.ACTIVE and not lease.is_active(now):
                lease.lease_state = LeaseState.EXPIRED
