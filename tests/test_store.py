import ipaddress

import pytest

from store.store import Classifier, Store

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


def test_temporary_lease_lifecycle():
    store = Store()
    lease = store.obtain_temporary_lease(GENERAL, N("10.4.0.0/24"), "test")
    assert lease.classifier == GENERAL
    assert store.get_active_allocations(GENERAL) == [lease]
    assert store.get_active_allocations(GENERAL, include_temporary=False) == []

    store.commit_temporary_lease(lease)
    assert store.temporary_lease[GENERAL] == []
    assert store.get_active_allocations(GENERAL, include_temporary=False) == [lease]
