"""REST API for the CIDR allocator.

Run from the repo root:

    python src/api.py              # http://127.0.0.1:5000
    python src/api.py --demo       # start with a few example allocations

All state is in memory and is lost when the server stops. See README.md for the endpoints.
"""
import argparse
import functools
import ipaddress
import threading

from flask import Flask, Response, jsonify, request, url_for
from werkzeug.exceptions import HTTPException

from cidr.cidr import NoAvailableNetworkError, find_available_network, get_classifier_from_ip, seed_demo
from store.store import (
    LEASE_TTL_SECONDS,
    Allocation,
    AllocationNotFoundError,
    Classifier,
    LeaseExpiredError,
    NetworkConflictError,
    Store,
)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def parse_prefix(value) -> int:
    """Accept 24, "24" or "/24"."""
    if isinstance(value, bool):
        value = None  # bool is an int subclass; True is not a prefix
    if isinstance(value, str) and value.strip().lstrip("/").isdigit():
        value = int(value.strip().lstrip("/"))
    if not isinstance(value, int) or not 1 <= value <= 32:
        raise ApiError(400, "'prefix' must be a subnet size from 1 to 32, e.g. 24 or \"/24\"")
    return value


def parse_network(text: str) -> ipaddress.IPv4Network:
    try:
        return ipaddress.IPv4Network(text)
    except ValueError as e:
        raise ApiError(400, f"Invalid network {text!r}: {e}") from None


def create_app(store: Store | None = None) -> Flask:
    store = store or Store()
    app = Flask(__name__)
    app.json.sort_keys = False
    app.extensions["cidr_store"] = store

    # The store isn't thread-safe and Flask serves requests on several threads, so every
    # request that touches it runs under one lock. That also makes "find a free block, then
    # lease it" atomic.
    lock = threading.Lock()
    # Leases swept by expiry, by network, so confirming one late gets a 410 instead of a 404.
    expired_leases: dict[str, Allocation] = {}

    def with_store(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            with lock:
                for lease in store.expire_temporary_leases():
                    expired_leases[str(lease.network)] = lease
                return view(*args, **kwargs)
        return wrapper

    # --- helpers ---

    def classifier_named(name: str | None, status: int = 400) -> Classifier:
        for classifier in store.roots:
            if classifier.name == name:
                return classifier
        names = ", ".join(c.name for c in store.roots)
        raise ApiError(status, f"Unknown class {name!r}. Expected one of: {names}")

    def classifier_for(network: ipaddress.IPv4Network) -> Classifier:
        try:
            return get_classifier_from_ip(network, store.roots)
        except ValueError:
            raise ApiError(404, f"{network} is not inside any root network") from None

    def allocation_json(allocation: Allocation) -> dict:
        body = {
            "network": str(allocation.network),
            "class": allocation.classifier.name,
            "assignee": allocation.assignee,
            "addresses": allocation.network.num_addresses,
            "status": "pending" if allocation.is_pending_lease else "allocated",
            "begin_time": allocation.begin_time,
        }
        if allocation.is_pending_lease:
            assert allocation.expires_at is not None
            body["expires_at"] = allocation.expires_at
            body["seconds_left"] = max(0, allocation.expires_at - store.clock())
            body["confirm_url"] = url_for("confirm_lease", network=str(allocation.network))
        return body

    def class_json(classifier: Classifier) -> dict:
        roots = store.get_roots(classifier)
        active = store.get_active_allocations(classifier)
        total = sum(root.num_addresses for root in roots)
        used = sum(a.network.num_addresses for a in active)
        return {
            "name": classifier.name,
            "pci": classifier.pci,
            "hub": classifier.hub,
            "environment": classifier.environment,
            "roots": [str(root) for root in roots],
            "total_addresses": total,
            "used_addresses": used,
            "used_fraction": round(used / total, 4),
            "pending_leases": sum(1 for a in active if a.is_pending_lease),
        }

    def active_allocations(class_name: str | None) -> list[Allocation]:
        classifiers = [classifier_named(class_name)] if class_name else list(store.roots)
        return [
            allocation
            for classifier in classifiers
            for allocation in sorted(store.get_active_allocations(classifier), key=lambda a: a.network)
        ]

    # --- errors: always JSON ---

    @app.errorhandler(ApiError)
    def handle_api_error(e: ApiError):
        return jsonify(error=e.message), e.status

    @app.errorhandler(AllocationNotFoundError)
    def handle_not_found(e):
        return jsonify(error=str(e)), 404

    @app.errorhandler(NoAvailableNetworkError)
    @app.errorhandler(NetworkConflictError)
    def handle_conflict(e):
        return jsonify(error=str(e)), 409

    @app.errorhandler(LeaseExpiredError)
    def handle_expired(e):
        return jsonify(error=str(e)), 410

    @app.errorhandler(ValueError)
    def handle_bad_request(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(HTTPException)
    def handle_http_error(e: HTTPException):
        return jsonify(error=e.description), e.code

    # --- routes ---

    @app.get("/")
    def index():
        return jsonify(
            service="cidr-allocator",
            lease_ttl_seconds=store.lease_ttl_seconds,
            endpoints={
                "GET /health": "liveness check",
                "GET /classes": "address classes, their roots and usage",
                "GET /classes/<name>": "one class",
                "GET /allocations[?class=<name>&status=pending|allocated]": "allocations and pending leases",
                "GET /allocations/<network>": "one allocation or pending lease",
                "DELETE /allocations/<network>": "release an allocation (or drop a pending lease)",
                "POST /leases": "propose the nearest free block: {\"class\": ..., \"prefix\": 24, \"assignee\": ...}",
                "GET /leases": "pending leases",
                "GET /leases/<network>": "one pending lease",
                "POST /leases/<network>/confirm": "confirm a pending lease before it expires",
                "DELETE /leases/<network>": "drop a pending lease",
            },
        )

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/classes")
    @with_store
    def list_classes():
        return jsonify(classes=[class_json(c) for c in store.roots])

    @app.get("/classes/<name>")
    @with_store
    def get_class(name: str):
        return jsonify(class_json(classifier_named(name, status=404)))

    @app.get("/allocations")
    @with_store
    def list_allocations():
        status = request.args.get("status")
        if status not in (None, "pending", "allocated"):
            raise ApiError(400, "'status' must be 'pending' or 'allocated'")
        rows = [allocation_json(a) for a in active_allocations(request.args.get("class"))]
        if status:
            rows = [row for row in rows if row["status"] == status]
        return jsonify(allocations=rows)

    @app.get("/allocations/<path:network>")
    @with_store
    def get_allocation(network: str):
        net = parse_network(network)
        classifier = classifier_for(net)
        allocation = store.get_allocation(classifier, net) or store.get_temporary_lease(classifier, net)
        if allocation is None:
            raise ApiError(404, f"No allocation or pending lease for {net}")
        return jsonify(allocation_json(allocation))

    @app.delete("/allocations/<path:network>")
    @with_store
    def release_allocation(network: str):
        net = parse_network(network)
        classifier = classifier_for(net)
        lease = store.get_temporary_lease(classifier, net)
        if lease is not None:
            store.cancel_temporary_lease(lease)
            return jsonify(network=str(net), status="dropped")
        allocation = store.release_allocation(classifier, net)
        return jsonify(network=str(net), status="released", assignee=allocation.assignee, end_time=allocation.end_time)

    @app.post("/leases")
    @with_store
    def create_lease():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "Send a JSON object, e.g. {\"class\": \"nonpci-spoke-prod\", \"prefix\": 24}")
        classifier = classifier_named(body.get("class"))
        prefix = parse_prefix(body.get("prefix"))
        assignee = body.get("assignee", "api")
        if not isinstance(assignee, str) or not assignee.strip():
            raise ApiError(400, "'assignee' must be a non-empty string")

        network = find_available_network(store, classifier, prefix)
        lease = store.obtain_temporary_lease(classifier, network, assignee.strip())
        expired_leases.pop(str(network), None)
        response = jsonify(allocation_json(lease))
        response.status_code = 201
        response.headers["Location"] = url_for("get_lease", network=str(network))
        return response

    @app.get("/leases")
    @with_store
    def list_leases():
        leases = [a for a in active_allocations(request.args.get("class")) if a.is_pending_lease]
        return jsonify(leases=[allocation_json(a) for a in leases])

    def pending_lease(network: str) -> Allocation:
        net = parse_network(network)
        classifier = classifier_for(net)
        lease = store.get_temporary_lease(classifier, net)
        if lease is not None:
            return lease
        if str(net) in expired_leases:
            raise LeaseExpiredError(f"The lease on {net} expired before it was confirmed. Request a new one.")
        if store.get_allocation(classifier, net):
            raise ApiError(409, f"{net} is already confirmed")
        raise ApiError(404, f"No pending lease for {net}")

    @app.get("/leases/<path:network>")
    @with_store
    def get_lease(network: str):
        return jsonify(allocation_json(pending_lease(network)))

    @app.post("/leases/<path:network>/confirm")
    @with_store
    def confirm_lease(network: str):
        lease = pending_lease(network)
        allocation = store.commit_temporary_lease(lease)
        return jsonify(allocation_json(allocation))

    @app.delete("/leases/<path:network>")
    @with_store
    def drop_lease(network: str):
        store.cancel_temporary_lease(pending_lease(network))
        return Response(status=204)

    return app


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="REST API for the CIDR allocator.")
    parser.add_argument("--host", default="127.0.0.1", help="interface to listen on (default: %(default)s)")
    parser.add_argument("--port", type=int, default=5000, help="port to listen on (default: %(default)s)")
    parser.add_argument(
        "--lease-ttl", type=int, default=LEASE_TTL_SECONDS, metavar="SECONDS",
        help="how long a proposed allocation is held before it expires (default: %(default)s)",
    )
    parser.add_argument("--demo", action="store_true", help="start with a few example allocations")
    args = parser.parse_args(argv)
    if args.lease_ttl <= 0:
        parser.error("--lease-ttl must be positive")

    store = Store(lease_ttl_seconds=args.lease_ttl)
    if args.demo:
        seed_demo(store)
    create_app(store).run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
