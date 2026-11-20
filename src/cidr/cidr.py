import ipaddress
import random

from store.store import Classifier, Allocation, Store

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

def find_available_network(store: 'Store', classifier: Classifier, subnet_mask: int) -> ipaddress.IPv4Network | None:
    """
    Find an available network for the given classifier and subnet mask. Optimize for contiguous allocation and avoid fragmentation.

    :param classifier: The Classifier object to find an available network for.
    :param subnet_mask: The desired subnet mask (as an integer).
    :return: An available IPv4Network object if found, None otherwise.
    """
    allocations = store.get_allocations(classifier)
    roots = store.get_roots(classifier)
    if not roots:
        raise ValueError(f"No root networks defined for classifier {classifier}")

    largest_root_size = min(root.prefixlen for root in store.get_roots(classifier))

    iteration = 0
    while iteration < subnet_mask - largest_root_size + 1:
        filtered_allocations = sorted([x for x in allocations if x.network.prefixlen <= subnet_mask - iteration], key=lambda a: a.network.prefixlen)  # Filter allocations by the desired subnet mask
        for allocation in filtered_allocations:
            parent_network = allocation.network.supernet(new_prefix=subnet_mask - iteration - 1)
            if not store.is_valid_network(classifier, parent_network):
                continue
            potential_subnets = list(parent_network.subnets(new_prefix=subnet_mask))
            for potential_subnet in potential_subnets:
                if not is_allocated(potential_subnet, allocations):
                    return potential_subnet
        iteration += 1

    root_subnets = []
    for root in roots:
        root_subnets.extend(list(root.subnets(new_prefix=subnet_mask)))
    random.shuffle(root_subnets)
    for root_subnet in root_subnets:
        if not is_allocated(root_subnet, allocations):
            return root_subnet

    raise ValueError(f"No available network found for classifier {classifier} with subnet mask {subnet_mask}")