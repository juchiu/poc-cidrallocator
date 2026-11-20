import ipaddress

from store.store import Classifier, Allocation, Store


class NoAvailableNetworkError(ValueError):
    """Raised when every root for a classifier is too full to fit the requested size."""

def is_allocated(network: ipaddress.IPv4Network, allocations: list[Allocation]) -> bool:
    """
    Check if the given network is already allocated in the provided allocations.

    :param network: The network to check.
    :param allocations: A list of Allocation objects to check against.
    :return: True if the network is already allocated, False otherwise.
    """
    for allocation in allocations:
        allocated_network = allocation.network
        if network.overlaps(allocated_network):
            return True
    return False

def get_classifier_from_ip(network: ipaddress.IPv4Network, roots: dict) -> Classifier:
    """
    Determine the classifier for a given IP address and subnet mask based on the provided root networks.

    :param network: The network to classify.
    :param roots: A dictionary mapping Classifier objects to lists of root networks.
    :return: The Classifier object that matches the given IP address and subnet mask.
    :raises ValueError: If no matching classifier is found.
    """
    for classifier, root_networks in roots.items():
        if any(network.subnet_of(root) for root in root_networks):
            return classifier
    raise ValueError(f"No matching classifier found for network {network}")

def find_available_network(store: 'Store', classifier: Classifier, subnet_mask: int) -> ipaddress.IPv4Network:
    """
    Find an available network for the given classifier and subnet mask. Optimize for contiguous allocation and avoid fragmentation.

    Pending (unexpired) temporary leases count as taken.

    :param classifier: The Classifier object to find an available network for.
    :param subnet_mask: The desired subnet mask (as an integer).
    :return: The closest free network to an existing allocation, or the first free one if none are nearby.
    :raises ValueError: If the classifier has no roots, the request is larger than every root, or nothing is free.
    """
    roots = store.get_roots(classifier)
    if not roots:
        raise ValueError(f"No root networks defined for class {classifier.name}")
    if all(root.prefixlen > subnet_mask for root in roots):
        raise ValueError(f"/{subnet_mask} is larger than every root network for class {classifier.name}")

    allocations = store.get_active_allocations(classifier)
    shortest_root_prefix = min(root.prefixlen for root in roots)

    # Walk outward from existing allocations: the /(mask-1) "buddy" block first,
    # then /(mask-2), ... up to the root. The first hit is the closest free block.
    for parent_prefix in range(subnet_mask - 1, shortest_root_prefix - 1, -1):
        # Only allocations smaller than the parent can leave free space inside it.
        neighbours = sorted(
            (a.network for a in allocations if a.network.prefixlen > parent_prefix),
            key=lambda n: (n.prefixlen, n),
        )
        seen = set()
        for network in neighbours:
            parent = network.supernet(new_prefix=parent_prefix)
            if parent in seen or not store.is_valid_network(classifier, parent):
                continue
            seen.add(parent)
            for candidate in parent.subnets(new_prefix=subnet_mask):
                if not is_allocated(candidate, allocations):
                    return candidate

    # Nothing free near an existing allocation: take the first free block, in root order.
    for root in roots:
        if root.prefixlen > subnet_mask:
            continue
        for candidate in root.subnets(new_prefix=subnet_mask):
            if not is_allocated(candidate, allocations):
                return candidate

    raise NoAvailableNetworkError(f"No available network found for class {classifier.name} with subnet mask {subnet_mask}")


def seed_demo(store: 'Store'):
    """Add a few example allocations so demos have something to show."""
    spoke = Classifier(pci=False, hub=False, environment="prod")
    pci_hub = Classifier(pci=True, hub=True, environment="prod")
    for classifier, prefix, assignee in [
        (spoke, 22, "payments-api"),
        (spoke, 24, "search"),
        (spoke, 26, "batch-jobs"),
        (spoke, 24, "analytics"),
        (pci_hub, 24, "pci-transit"),
    ]:
        store.add_allocation(classifier, find_available_network(store, classifier, prefix), assignee)