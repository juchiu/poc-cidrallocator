import ipaddress

import pytest

from store.store import Classifier, LeaseExpiredError, Store

N = ipaddress.ip_network

GENERAL = Classifier(pci=False, hub=False, environment="prod")


def test_store_constructs_with_classifier_keys():
    store = Store()
    assert store.get_roots(GENERAL) == [N("10.4.0.0/16"), N("10.6.0.0/16"), N("100.64.0.0/16")]


def test_add_allocation_outside_roots_raises():
    with pytest.raises(ValueError, match="not in any of the root networks"):
        Store().add_allocation(GENERAL, N("10.1.0.0/24"), "test")


def test_add_and_get_allocation():
    store = Store()
    store.add_allocation(GENERAL, N("10.4.0.0/24"), "team-a")
    allocation = store.get_allocation(GENERAL, N("10.4.0.0/24"))
    assert allocation is not None
    assert allocation.assignee == "team-a"
    assert allocation.end_time is None
    assert allocation.classifier == GENERAL


def test_release_removes_from_active():
    store = Store()
    store.add_allocation(GENERAL, N("10.4.0.0/24"), "test")
    store.release_allocation(GENERAL, N("10.4.0.0/24"))
    assert store.get_active_allocations(GENERAL) == []


def test_active_allocations_at_a_point_in_time():
    store = Store()
    store.add_allocation(GENERAL, N("10.4.0.0/24"), "test")
    allocation = store.data[GENERAL][0]
    allocation.begin_time, allocation.end_time = 100, 200

    assert store.get_active_allocations(GENERAL, timestamp=50) == []
    assert store.get_active_allocations(GENERAL, timestamp=150) == [allocation]
    assert store.get_active_allocations(GENERAL, timestamp=250) == []
    assert store.get_active_allocations(GENERAL) == []


def test_add_allocation_overlapping_existing_raises():
    store = Store()
    store.add_allocation(GENERAL, N("10.4.0.0/24"), "test")
    with pytest.raises(ValueError, match="overlaps"):
        store.add_allocation(GENERAL, N("10.4.0.128/25"), "test")
    with pytest.raises(ValueError, match="overlaps"):
        store.obtain_temporary_lease(GENERAL, N("10.4.0.0/16"), "test")


def test_released_allocation_is_not_allocated():
    store = Store()
    store.add_allocation(GENERAL, N("10.4.0.0/24"), "test")
    store.release_allocation(GENERAL, N("10.4.0.0/24"))
    assert not store.is_allocated(GENERAL, N("10.4.0.0/24"))


def test_release_targets_the_active_allocation_not_an_old_one():
    store = Store()
    store.add_allocation(GENERAL, N("10.4.0.0/24"), "first")
    store.release_allocation(GENERAL, N("10.4.0.0/24"))
    store.add_allocation(GENERAL, N("10.4.0.0/24"), "second")
    assert store.release_allocation(GENERAL, N("10.4.0.0/24")).assignee == "second"
    assert not store.is_allocated(GENERAL, N("10.4.0.0/24"))


def test_release_unknown_network_raises():
    with pytest.raises(ValueError, match="No active allocation"):
        Store().release_allocation(GENERAL, N("10.4.0.0/24"))


def test_lease_stops_blocking_space_at_its_deadline(clock):
    store = Store(clock=clock, lease_ttl_seconds=120)
    lease = store.obtain_temporary_lease(GENERAL, N("10.4.0.0/24"), "test")
    assert lease.expires_at == clock.now + 120
    clock.advance(119)
    assert store.is_allocated(GENERAL, N("10.4.0.0/24"))
    clock.advance(1)
    assert not store.is_allocated(GENERAL, N("10.4.0.0/24"))
    assert store.expire_temporary_leases() == [lease]
    assert store.temporary_lease[GENERAL] == []


def test_commit_after_deadline_raises(clock):
    store = Store(clock=clock, lease_ttl_seconds=120)
    lease = store.obtain_temporary_lease(GENERAL, N("10.4.0.0/24"), "test")
    clock.advance(120)
    with pytest.raises(LeaseExpiredError):
        store.commit_temporary_lease(lease)
    assert store.get_active_allocations(GENERAL) == []


def test_commit_before_deadline_clears_expiry(clock):
    store = Store(clock=clock, lease_ttl_seconds=120)
    lease = store.obtain_temporary_lease(GENERAL, N("10.4.0.0/24"), "test")
    clock.advance(119)
    store.commit_temporary_lease(lease)
    clock.advance(1000)
    assert not lease.is_pending_lease
    assert store.get_active_allocations(GENERAL) == [lease]


def test_cancel_lease_frees_space():
    store = Store()
    lease = store.obtain_temporary_lease(GENERAL, N("10.4.0.0/24"), "test")
    store.cancel_temporary_lease(lease)
    assert not store.is_allocated(GENERAL, N("10.4.0.0/24"))
    with pytest.raises(ValueError, match="not a pending lease"):
        store.commit_temporary_lease(lease)


def test_temporary_lease_lifecycle():
    store = Store()
    lease = store.obtain_temporary_lease(GENERAL, N("10.4.0.0/24"), "test")
    assert lease.classifier == GENERAL
    assert store.get_active_allocations(GENERAL) == [lease]
    assert store.get_active_allocations(GENERAL, include_temporary=False) == []

    store.commit_temporary_lease(lease)
    assert store.temporary_lease[GENERAL] == []
    assert store.get_active_allocations(GENERAL, include_temporary=False) == [lease]
