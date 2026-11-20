import io

import pytest

from main import CidrShell, main, seed_demo
from store.store import Classifier, Store

GENERAL = Classifier(pci=False, hub=False, environment="prod")


@pytest.fixture
def store(clock) -> Store:
    return Store(clock=clock, lease_ttl_seconds=120)


def run(store: Store, line: str, answers: str = "") -> str:
    """Run one shell command, feeding `answers` to any prompts, and return its output."""
    out = io.StringIO()
    shell = CidrShell(store, stdin=io.StringIO(answers), stdout=out)
    shell.onecmd(shell.precmd(line))
    return out.getvalue()


def test_request_and_confirm_immediately(store):
    output = run(store, "request nonpci-spoke-prod /24 web", answers="y\n")
    assert "Proposed 10.4.0.0/24 for web" in output
    assert "Confirmed 10.4.0.0/24 for web." in output
    assert store.get_allocation(GENERAL, store.data[GENERAL][0].network).assignee == "web"


def test_request_prompts_for_missing_values(store):
    output = run(store, "request", answers="4\n/26\ny\n")
    assert "Proposed 10.4.0.0/26 for demo" in output
    assert "Confirmed 10.4.0.0/26" in output


def test_request_decline_frees_the_space(store):
    output = run(store, "request 4 24", answers="n\n")
    assert "Dropped the lease; 10.4.0.0/24 is free again." in output
    assert store.get_active_allocations(GENERAL) == []


def test_pending_lease_can_be_confirmed_later(store, clock):
    run(store, "request 4 24", answers="\n")
    clock.advance(60)
    assert "pending, 1:00 left to confirm" in run(store, "list")
    assert "Confirmed 10.4.0.0/24" in run(store, "confirm 10.4.0.0/24")
    assert "allocated" in run(store, "list")


def test_pending_lease_expires_after_ttl(store, clock):
    run(store, "request 4 24", answers="later\n")
    clock.advance(120)
    output = run(store, "confirm 10.4.0.0/24")
    assert "Expired: the lease on 10.4.0.0/24" in output
    assert "No pending lease for 10.4.0.0/24" in output
    # The space is free again, so the next request gets the same block.
    assert "Proposed 10.4.0.0/24" in run(store, "request 4 24", answers="n\n")


def test_confirming_at_the_prompt_after_the_deadline_fails(store, clock):
    class SlowAnswer(io.StringIO):
        def readline(self, *args):
            clock.advance(121)  # the user takes too long to answer
            return super().readline(*args)

    out = io.StringIO()
    shell = CidrShell(store, stdin=SlowAnswer("y\n"), stdout=out)
    shell.onecmd("request 4 24")
    assert "Too late: the lease on 10.4.0.0/24 expired" in out.getvalue()
    assert store.get_active_allocations(GENERAL) == []


def test_pending_lease_blocks_its_space(store):
    run(store, "request 4 24", answers="later\n")
    assert "Proposed 10.4.1.0/24" in run(store, "request 4 24", answers="n\n")


def test_release_lets_the_block_be_reallocated(store):
    run(store, "request 4 24 first", answers="y\n")
    run(store, "request 4 24 second", answers="y\n")
    assert "Released 10.4.0.0/24 (was first)" in run(store, "release 10.4.0.0/24")
    assert "Proposed 10.4.0.0/24 for third" in run(store, "request 4 24 third", answers="y\n")


def test_release_drops_a_pending_lease(store):
    run(store, "request 4 24", answers="later\n")
    assert "Dropped the pending lease on 10.4.0.0/24" in run(store, "release 10.4.0.0/24")
    assert store.get_active_allocations(GENERAL) == []


def test_map_draws_allocations_and_leases(store):
    run(store, "request 4 24", answers="y\n")
    run(store, "request 4 26", answers="later\n")
    output = run(store, "map nonpci-spoke-prod")
    first_row = next(line for line in output.splitlines() if line.strip().startswith("10.4.0.0 "))
    assert first_row.split()[-1].startswith("█▒·")


def test_map_zooms_into_a_network(store):
    run(store, "request 4 26", answers="y\n")
    output = run(store, "map 10.4.0.0/24")
    assert "(1 cell = /32, 1 addresses)" in output
    assert "25.0% used" in output


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("request bogus 24", "Unknown class 'bogus'"),
        ("request 4 /33", "Subnet size must be /1 to /32"),
        ("request 4 15", "larger than every root"),
        ("confirm 10.9.0.0/24", "not inside any root network"),
        ("release 10.4.0.0/24", "No active allocation for 10.4.0.0/24"),
        ("release 10.4.0.1/24", "has host bits set"),
        ("frobnicate", "Unknown command 'frobnicate'"),
    ],
)
def test_bad_input_shows_an_error(store, line, message):
    output = run(store, line)
    assert "Error:" in output
    assert message in output


def test_demo_seed_and_classes(store):
    seed_demo(store)
    output = run(store, "classes")
    assert "nonpci-spoke-prod" in output
    assert "10.4.0.0/16, 10.6.0.0/16, 100.64.0.0/16" in output
    assert len(store.get_active_allocations(GENERAL)) == 4


def test_main_runs_a_session(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("request 4 24\ny\nlist\nquit\n"))
    assert main(["--demo", "--no-color"]) == 0
    output = capsys.readouterr().out
    assert output.index("CIDR allocator demo") < output.index("nonpci-spoke-prod")
    assert "Confirmed 10.4.7.0/24 for demo." in output


def test_main_rejects_non_positive_ttl():
    with pytest.raises(SystemExit):
        main(["--lease-ttl", "0"])
