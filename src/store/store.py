import datetime
import ipaddress
import pydantic

class Classifier(pydantic.BaseModel):
    pci: bool
    hub: bool
    environment: str

class Allocation(pydantic.BaseModel):
    network: ipaddress.IPv4Network
    assignee: str
    begin_time: int
    end_time: int | None
    classifier: Classifier

class Store:
    def __init__(self):
        self.roots: dict[Classifier, list[ipaddress.IPv4Network]] = {
            Classifier(pci=True, hub=True, environment="prod"): [
                ipaddress.ip_network("10.1.0.0/16"),
            ],
            Classifier(pci=True, hub=False, environment="prod"): [
                ipaddress.ip_network("10.2.0.0/16"),
            ],
            Classifier(pci=False, hub=True, environment="prod"): [
                ipaddress.ip_network("10.3.0.0/16"),
            ],
            Classifier(pci=False, hub=False, environment="prod"): [
                ipaddress.ip_network("10.4.0.0/16"),
                ipaddress.ip_network("10.6.0.0/16"),
                ipaddress.ip_network("100.64.0.0/16"),
            ],
        }
        self.data: dict[Classifier, list[Allocation]] = {classifier: [] for classifier in self.roots}
        self.temporary_lease: dict[Classifier, list[Allocation]] = {classifier: [] for classifier in self.roots}

    def get_roots(self, classifier: Classifier) -> list[ipaddress.IPv4Network]:
        return self.roots.get(classifier, [])

    def is_valid_network(self, classifier: Classifier, network: ipaddress.IPv4Network) -> bool:
        return any(network.subnet_of(root) for root in self.roots.get(classifier, []))

    def add_allocation(self, classifier: Classifier, network: ipaddress.IPv4Network, assignee: str):
        # Validate the IP address and subnet
        try:
            if not self.is_valid_network(classifier, network):
                raise ValueError(f"Network {network} is not in any of the root networks for classifier {classifier}")
        except ValueError as e:
            raise ValueError(f"Invalid IP address or subnet: {e}")

        allocation = Allocation(network=network, assignee=assignee, begin_time=int(datetime.datetime.now().timestamp()), classifier=classifier)
        self.data[classifier].append(allocation)

    def get_allocation(self, classifier: Classifier, network: ipaddress.IPv4Network) -> Allocation | None:
        for allocation in self.data[classifier]:
            if allocation.network == network:
                return allocation
        return None

    def is_allocated(self, classifier: Classifier, ip_network: ipaddress.IPv4Network) -> bool:
        for allocation in self.data[classifier]:
            if ip_network.overlaps(allocation.network):
                return True
        return False

    def release_allocation(self, classifier: Classifier, network: ipaddress.IPv4Network):
        for allocation in self.data[classifier]:
            if allocation.network == network:
                allocation.end_time = int(datetime.datetime.now().timestamp())
                break

    def get_active_allocations(self, classifier: Classifier, timestamp: int | None = None, include_temporary: bool = True) -> list[Allocation]:
        if not timestamp:
            timestamp = int(datetime.datetime.now().timestamp())
        active = [allocation for allocation in self.data[classifier] if allocation.end_time is None and allocation.begin_time <= timestamp]
        if include_temporary:
            active += [allocation for allocation in self.temporary_lease[classifier]     if allocation.end_time is None and allocation.begin_time <= timestamp]
        return active

    def obtain_temporary_lease(self, classifier: Classifier, network: ipaddress.IPv4Network, assignee: str) -> Allocation:
        begin_time = int(datetime.datetime.now().timestamp())
        temporary_allocation = Allocation(network=network, assignee=assignee, begin_time=begin_time)
        self.temporary_lease[classifier].append(temporary_allocation)
        return temporary_allocation

    def commit_temporary_lease(self, allocation: Allocation):
        classifier = allocation.classifier
        self.data[classifier].append(allocation)
        self.temporary_lease[classifier].remove(allocation)