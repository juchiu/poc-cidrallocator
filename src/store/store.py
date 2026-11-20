import datetime
import ipaddress
from collections.abc import Callable

import pydantic

# How long a proposed allocation is held before it lapses unless confirmed.
LEASE_TTL_SECONDS = 120


class LeaseExpiredError(ValueError):
    """Raised when a temporary lease is confirmed after its deadline."""


def wall_clock() -> int:
    return int(datetime.datetime.now().timestamp())


class Classifier(pydantic.BaseModel):
    # Frozen so it is hashable and can be used as a dict key.
    model_config = pydantic.ConfigDict(frozen=True)

    pci: bool
    hub: bool
    environment: str

    @property
    def name(self) -> str:
        """Short human-readable name, e.g. "pci-hub-prod" or "nonpci-spoke-prod"."""
        return f"{'pci' if self.pci else 'nonpci'}-{'hub' if self.hub else 'spoke'}-{self.environment}"


class Allocation(pydantic.BaseModel):
    network: ipaddress.IPv4Network
    assignee: str
    begin_time: int
    end_time: int | None = None
    classifier: Classifier
    # Set only while this is a pending temporary lease: the time it lapses unless confirmed.
    expires_at: int | None = None

    @property
    def is_pending_lease(self) -> bool:
        return self.expires_at is not None

    def is_active_at(self, timestamp: int) -> bool:
        return (
            self.begin_time <= timestamp
            and (self.end_time is None or self.end_time > timestamp)
            and (self.expires_at is None or self.expires_at > timestamp)
        )


class Store:
    def __init__(self, clock: Callable[[], int] = wall_clock, lease_ttl_seconds: int = LEASE_TTL_SECONDS):
        """
        :param clock: Returns the current time in epoch seconds. Injectable so tests can control time.
        :param lease_ttl_seconds: How long a temporary lease is held before it expires.
        """
        self.clock = clock
        self.lease_ttl_seconds = lease_ttl_seconds
        self.roots: dict[Classifier, list[ipaddress.IPv4Network]] = {
            Classifier(pci=True, hub=True, environment="prod"): [
                ipaddress.IPv4Network("10.1.0.0/16"),
            ],
            Classifier(pci=True, hub=False, environment="prod"): [
                ipaddress.IPv4Network("10.2.0.0/16"),
            ],
            Classifier(pci=False, hub=True, environment="prod"): [
                ipaddress.IPv4Network("10.3.0.0/16"),
            ],
            Classifier(pci=False, hub=False, environment="prod"): [
                ipaddress.IPv4Network("10.4.0.0/16"),
                ipaddress.IPv4Network("10.6.0.0/16"),
                ipaddress.IPv4Network("100.64.0.0/16"),
            ],
        }
        self.data: dict[Classifier, list[Allocation]] = {classifier: [] for classifier in self.roots}
        self.temporary_lease: dict[Classifier, list[Allocation]] = {classifier: [] for classifier in self.roots}

    def get_roots(self, classifier: Classifier) -> list[ipaddress.IPv4Network]:
        return self.roots.get(classifier, [])

    def is_valid_network(self, classifier: Classifier, network: ipaddress.IPv4Network) -> bool:
        return any(network.subnet_of(root) for root in self.roots.get(classifier, []))

    def _check_can_allocate(self, classifier: Classifier, network: ipaddress.IPv4Network):
        if not self.is_valid_network(classifier, network):
            raise ValueError(f"Network {network} is not in any of the root networks for classifier {classifier}")
        if self.is_allocated(classifier, network):
            raise ValueError(f"Network {network} overlaps an existing allocation or pending lease")

    def add_allocation(self, classifier: Classifier, network: ipaddress.IPv4Network, assignee: str) -> Allocation:
        """Allocate a network directly, without a temporary lease."""
        self._check_can_allocate(classifier, network)
        allocation = Allocation(network=network, assignee=assignee, begin_time=self.clock(), classifier=classifier)
        self.data[classifier].append(allocation)
        return allocation

    def get_allocation(self, classifier: Classifier, network: ipaddress.IPv4Network) -> Allocation | None:
        """Return the active (unreleased) allocation for exactly this network, if any."""
        for allocation in self.data[classifier]:
            if allocation.network == network and allocation.end_time is None:
                return allocation
        return None

    def is_allocated(self, classifier: Classifier, ip_network: ipaddress.IPv4Network) -> bool:
        """True if the network overlaps an active allocation or an unexpired pending lease."""
        return any(ip_network.overlaps(allocation.network) for allocation in self.get_active_allocations(classifier))

    def release_allocation(self, classifier: Classifier, network: ipaddress.IPv4Network) -> Allocation:
        allocation = self.get_allocation(classifier, network)
        if allocation is None:
            raise ValueError(f"No active allocation for {network}")
        allocation.end_time = self.clock()
        return allocation

    def get_active_allocations(self, classifier: Classifier, timestamp: int | None = None, include_temporary: bool = True) -> list[Allocation]:
        """
        With no timestamp, return every allocation that has not been released, plus pending leases
        that have not expired. This deliberately ignores begin_time: if the wall clock steps
        backwards, a just-made allocation can look like it starts in the future, and skipping it
        would let its space be handed out twice.

        With a timestamp, return the allocations that were active at that moment.
        """
        committed = self.data[classifier]
        leases = self.temporary_lease[classifier] if include_temporary else []
        if timestamp is None:
            now = self.clock()
            return [a for a in committed if a.end_time is None] + [
                lease for lease in leases if lease.end_time is None and not self._lease_expired(lease, now)
            ]
        return [allocation for allocation in committed + leases if allocation.is_active_at(timestamp)]

    # --- Temporary leases: a proposed allocation held for lease_ttl_seconds until confirmed. ---

    def _lease_expired(self, lease: Allocation, now: int) -> bool:
        return lease.expires_at is not None and now >= lease.expires_at

    def _remove_lease(self, lease: Allocation):
        leases = self.temporary_lease[lease.classifier]
        for i, pending in enumerate(leases):
            if pending is lease:
                del leases[i]
                return
        raise ValueError(f"{lease.network} is not a pending lease")

    def obtain_temporary_lease(self, classifier: Classifier, network: ipaddress.IPv4Network, assignee: str) -> Allocation:
        self._check_can_allocate(classifier, network)
        now = self.clock()
        lease = Allocation(
            network=network,
            assignee=assignee,
            begin_time=now,
            classifier=classifier,
            expires_at=now + self.lease_ttl_seconds,
        )
        self.temporary_lease[classifier].append(lease)
        return lease

    def get_temporary_lease(self, classifier: Classifier, network: ipaddress.IPv4Network) -> Allocation | None:
        for lease in self.temporary_lease[classifier]:
            if lease.network == network:
                return lease
        return None

    def commit_temporary_lease(self, allocation: Allocation) -> Allocation:
        """Confirm a pending lease. Raises LeaseExpiredError if its deadline has passed."""
        self._remove_lease(allocation)
        if self._lease_expired(allocation, self.clock()):
            allocation.end_time = allocation.expires_at
            raise LeaseExpiredError(f"The lease on {allocation.network} expired before it was confirmed")
        allocation.expires_at = None
        self.data[allocation.classifier].append(allocation)
        return allocation

    def cancel_temporary_lease(self, allocation: Allocation) -> Allocation:
        self._remove_lease(allocation)
        allocation.end_time = self.clock()
        return allocation

    def expire_temporary_leases(self) -> list[Allocation]:
        """Drop every lease past its deadline and return them. Expired leases already stop
        blocking space without this; it just cleans up and lets callers report what lapsed."""
        now = self.clock()
        expired = []
        for leases in self.temporary_lease.values():
            keep = []
            for lease in leases:
                if self._lease_expired(lease, now):
                    lease.end_time = lease.expires_at
                    expired.append(lease)
                else:
                    keep.append(lease)
            leases[:] = keep
        return expired
