import threading

import pytest

from api import create_app
from cidr.cidr import seed_demo
from store.store import Store


@pytest.fixture
def store(clock) -> Store:
    return Store(clock=clock, lease_ttl_seconds=120)


@pytest.fixture
def client(store):
    return create_app(store).test_client()


def lease(client, prefix=24, cls="nonpci-spoke-prod", **extra):
    return client.post("/leases", json={"class": cls, "prefix": prefix, **extra})


def allocate(client, prefix=24, **extra) -> str:
    network = lease(client, prefix, **extra).get_json()["network"]
    assert client.post(f"/leases/{network}/confirm").status_code == 200
    return network


def test_index_and_health(client):
    assert client.get("/health").get_json() == {"status": "ok"}
    body = client.get("/").get_json()
    assert body["lease_ttl_seconds"] == 120
    assert "POST /leases" in body["endpoints"]


def test_classes(client, store):
    seed_demo(store)
    classes = {c["name"]: c for c in client.get("/classes").get_json()["classes"]}
    assert classes["nonpci-spoke-prod"]["roots"] == ["10.4.0.0/16", "10.6.0.0/16", "100.64.0.0/16"]
    assert classes["pci-hub-prod"]["used_addresses"] == 256
    assert client.get("/classes/pci-hub-prod").get_json()["roots"] == ["10.1.0.0/16"]
    assert client.get("/classes/nope").status_code == 404


def test_create_lease(client):
    response = lease(client, 24, assignee="web")
    assert response.status_code == 201
    assert response.headers["Location"] == "/leases/10.4.0.0/24"
    body = response.get_json()
    assert body == {
        "network": "10.4.0.0/24",
        "class": "nonpci-spoke-prod",
        "assignee": "web",
        "addresses": 256,
        "status": "pending",
        "begin_time": body["begin_time"],
        "expires_at": body["begin_time"] + 120,
        "seconds_left": 120,
        "confirm_url": "/leases/10.4.0.0/24/confirm",
    }


def test_prefix_accepts_int_and_strings(client):
    assert lease(client, 24).get_json()["network"] == "10.4.0.0/24"
    assert lease(client, "/24").get_json()["network"] == "10.4.1.0/24"
    assert lease(client, "26").get_json()["network"] == "10.4.2.0/26"


def test_confirm_lease(client):
    lease(client, 24)
    response = client.post("/leases/10.4.0.0/24/confirm")
    assert response.status_code == 200
    assert response.get_json()["status"] == "allocated"
    assert "expires_at" not in response.get_json()
    assert client.get("/allocations/10.4.0.0/24").get_json()["status"] == "allocated"
    assert client.post("/leases/10.4.0.0/24/confirm").status_code == 409


def test_confirm_after_expiry_is_gone(client, clock):
    lease(client, 24)
    clock.advance(119)
    assert client.get("/leases/10.4.0.0/24").get_json()["seconds_left"] == 1
    clock.advance(1)
    response = client.post("/leases/10.4.0.0/24/confirm")
    assert response.status_code == 410
    assert "expired" in response.get_json()["error"]
    assert client.get("/allocations").get_json() == {"allocations": []}


def test_expired_space_can_be_leased_again(client, clock):
    lease(client, 24)
    clock.advance(120)
    assert lease(client, 24).get_json()["network"] == "10.4.0.0/24"
    # The new lease replaces the expired one, so confirming it works.
    assert client.post("/leases/10.4.0.0/24/confirm").status_code == 200


def test_pending_lease_blocks_its_space(client):
    lease(client, 24)
    assert lease(client, 24).get_json()["network"] == "10.4.1.0/24"


def test_drop_lease(client):
    lease(client, 24)
    assert client.delete("/leases/10.4.0.0/24").status_code == 204
    assert client.get("/leases").get_json() == {"leases": []}
    assert client.delete("/leases/10.4.0.0/24").status_code == 404


def test_release_and_reallocate(client):
    allocate(client, 24, assignee="first")
    allocate(client, 24, assignee="second")
    response = client.delete("/allocations/10.4.0.0/24")
    assert response.status_code == 200
    assert response.get_json()["status"] == "released"
    assert response.get_json()["assignee"] == "first"
    assert lease(client, 24).get_json()["network"] == "10.4.0.0/24"


def test_release_drops_a_pending_lease(client):
    lease(client, 24)
    assert client.delete("/allocations/10.4.0.0/24").get_json()["status"] == "dropped"


def test_list_allocations_filters(client):
    allocate(client, 24)
    lease(client, 24)
    allocate(client, 24, cls="pci-hub-prod")
    all_rows = client.get("/allocations").get_json()["allocations"]
    assert [r["network"] for r in all_rows] == ["10.1.0.0/24", "10.4.0.0/24", "10.4.1.0/24"]
    spoke = client.get("/allocations?class=nonpci-spoke-prod").get_json()["allocations"]
    assert len(spoke) == 2
    pending = client.get("/allocations?status=pending").get_json()["allocations"]
    assert [r["network"] for r in pending] == ["10.4.1.0/24"]
    assert [r["network"] for r in client.get("/leases").get_json()["leases"]] == ["10.4.1.0/24"]


@pytest.mark.parametrize(
    ("method", "path", "json", "status", "message"),
    [
        ("post", "/leases", None, 400, "Send a JSON object"),
        ("post", "/leases", {"class": "nope", "prefix": 24}, 400, "Unknown class 'nope'"),
        ("post", "/leases", {"class": "nonpci-spoke-prod"}, 400, "'prefix' must be"),
        ("post", "/leases", {"class": "nonpci-spoke-prod", "prefix": 33}, 400, "'prefix' must be"),
        ("post", "/leases", {"class": "nonpci-spoke-prod", "prefix": True}, 400, "'prefix' must be"),
        ("post", "/leases", {"class": "nonpci-spoke-prod", "prefix": 15}, 400, "larger than every root"),
        ("post", "/leases", {"class": "nonpci-spoke-prod", "prefix": 24, "assignee": ""}, 400, "'assignee'"),
        ("get", "/allocations?class=nope", None, 400, "Unknown class"),
        ("get", "/allocations?status=weird", None, 400, "'status' must be"),
        ("get", "/allocations/10.4.0.1/24", None, 400, "has host bits set"),
        ("get", "/allocations/10.4.0.0/24", None, 404, "No allocation or pending lease"),
        ("get", "/allocations/192.168.0.0/24", None, 404, "not inside any root network"),
        ("delete", "/allocations/10.4.0.0/24", None, 404, "No active allocation"),
        ("post", "/leases/10.4.0.0/24/confirm", None, 404, "No pending lease"),
        ("get", "/nowhere", None, 404, "not found"),
        ("put", "/leases", None, 405, "not allowed"),
    ],
)
def test_errors_are_json(client, method, path, json, status, message):
    response = getattr(client, method)(path, json=json)
    assert response.status_code == status
    assert message in response.get_json()["error"]


def test_exhausted_class_returns_409(client, store):
    from store.store import Classifier
    pci_hub = Classifier(pci=True, hub=True, environment="prod")
    store.add_allocation(pci_hub, store.get_roots(pci_hub)[0], "everything")
    response = lease(client, 24, cls="pci-hub-prod")
    assert response.status_code == 409
    assert "No available network" in response.get_json()["error"]


def test_concurrent_requests_never_overlap(store):
    app = create_app(store)
    networks, errors = [], []

    def worker():
        client = app.test_client()
        for _ in range(25):
            response = client.post("/leases", json={"class": "nonpci-spoke-prod", "prefix": 26})
            if response.status_code != 201:
                errors.append(response.get_json())
            else:
                networks.append(response.get_json()["network"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(networks) == len(set(networks)) == 200
