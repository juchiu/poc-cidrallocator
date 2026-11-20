import ipaddress
import random

import pytest

from cidr.cidr import find_available_network, get_classifier_from_ip, is_allocated
from store.store import Classifier, Store

N = ipaddress.ip_network

# Roots for this classifier: 10.4.0.0/16, 10.6.0.0/16, 100.64.0.0/16 (in that order).
GENERAL = Classifier(pci=False, hub=False, environment="prod")
# Root for this classifier: 10.1.0.0/16.
PCI_HUB = Classifier(pci=True, hub=True, environment="prod")


def store_with(*networks: str, classifier: Classifier = GENERAL) -> Store:
    store = Store()
    for network in networks:
        store.add_allocation(classifier, N(network), "test")
    return store


@pytest.mark.parametrize(
    ("existing", "mask", "expected"),
    [
        pytest.param([], 24, "10.4.0.0/24", id="empty-root-takes-first-block"),
        pytest.param(["10.4.0.0/24"], 24, "10.4.1.0/24", id="same-size-buddy"),
        pytest.param(["10.4.0.0/20"], 24, "10.4.16.0/24", id="larger-existing-allocation"),
        pytest.param(["10.4.128.0/26"], 24, "10.4.129.0/24", id="smaller-existing-allocation"),
        pytest.param(["10.4.128.0/24", "10.4.0.0/22"], 24, "10.4.129.0/24", id="mixed-sizes"),
        pytest.param(["10.4.0.0/26", "10.4.0.128/25"], 26, "10.4.0.64/26", id="fills-hole"),
        pytest.param(["10.4.0.0/16"], 24, "10.6.0.0/24", id="full-root-spills-to-next-root"),
    ],
)
def test_finds_nearest_free_block(existing, mask, expected):
    store = store_with(*existing)
    assert find_available_network(store, GENERAL, mask) == N(expected)


def test_classifiers_use_their_own_roots():
    store = store_with("10.4.0.0/24")
    assert find_available_network(store, PCI_HUB, 24) == N("10.1.0.0/24")


def test_request_larger_than_every_root_raises():
    with pytest.raises(ValueError, match="larger than every root"):
        find_available_network(Store(), GENERAL, 15)


def test_all_roots_full_raises():
    store = store_with("10.4.0.0/16", "10.6.0.0/16", "100.64.0.0/16")
    with pytest.raises(ValueError, match="No available network"):
        find_available_network(store, GENERAL, 24)


def test_unknown_classifier_raises():
    unknown = Classifier(pci=False, hub=False, environment="dev")
    with pytest.raises(ValueError, match="No root networks"):
        find_available_network(Store(), unknown, 24)


def test_released_block_is_reused():
    store = store_with("10.4.0.0/24", "10.4.1.0/24")
    store.release_allocation(GENERAL, N("10.4.0.0/24"))
    assert find_available_network(store, GENERAL, 24) == N("10.4.0.0/24")


def test_temporary_lease_is_respected():
    store = store_with("10.4.1.0/24")
    store.obtain_temporary_lease(GENERAL, N("10.4.0.0/24"), "test")
    assert find_available_network(store, GENERAL, 24) == N("10.4.2.0/24")


def test_committed_lease_stays_allocated():
    store = Store()
    lease = store.obtain_temporary_lease(GENERAL, N("10.4.0.0/24"), "test")
    store.commit_temporary_lease(lease)
    assert store.is_allocated(GENERAL, N("10.4.0.0/24"))
    assert find_available_network(store, GENERAL, 24) == N("10.4.1.0/24")


def test_allocation_dated_in_future_still_blocks_space():
    # Simulates the wall clock stepping backwards between allocations.
    store = store_with("10.4.0.0/24")
    store.data[GENERAL][0].begin_time += 5
    assert find_available_network(store, GENERAL, 24) == N("10.4.1.0/24")


@pytest.mark.parametrize("seed", range(5))
def test_random_requests_never_overlap_and_stay_packed(seed):
    rng = random.Random(seed)
    store = Store()
    for _ in range(400):
        network = find_available_network(store, GENERAL, rng.choice([22, 24, 25, 26, 27, 28]))
        assert not store.is_allocated(GENERAL, network), f"{network} overlaps an existing allocation"
        store.add_allocation(GENERAL, network, "test")

    root = N("10.4.0.0/16")
    networks = [a.network for a in store.get_active_allocations(GENERAL) if a.network.subnet_of(root)]
    used = sum(n.num_addresses for n in networks)
    span = int(max(n.broadcast_address for n in networks)) - int(root.network_address) + 1
    assert used / span >= 0.99


def test_is_allocated_detects_overlap():
    allocations = store_with("10.4.0.0/24").get_active_allocations(GENERAL)
    assert is_allocated(N("10.4.0.128/25"), allocations)
    assert is_allocated(N("10.4.0.0/16"), allocations)
    assert not is_allocated(N("10.4.1.0/24"), allocations)


def test_get_classifier_from_ip():
    roots = Store().roots
    assert get_classifier_from_ip(N("10.1.5.0/24"), roots) == PCI_HUB
    assert get_classifier_from_ip(N("100.64.0.0/20"), roots) == GENERAL
    with pytest.raises(ValueError, match="No matching classifier"):
        get_classifier_from_ip(N("192.168.0.0/24"), roots)
