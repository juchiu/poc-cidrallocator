# CIDR allocator (proof of concept)

Hands out IPv4 subnets from a fixed set of root ranges. Each new subnet goes as close as possible
to the ones already allocated, so used space stays contiguous and large free blocks stay intact.
A request is first held as a **temporary lease** for 2 minutes. It becomes a real allocation only
if it is confirmed within that time; otherwise it expires and the space is free again.

Everything is held in memory. This is a proof of concept, not a service.

## Quick start

Requires Python 3.10 or later.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'

python src/main.py --demo
```

`--demo` starts with a few example allocations so the maps have something to show. Leave it off
to start empty.

| Option | Meaning |
|---|---|
| `--demo` | Start with a few example allocations |
| `--lease-ttl SECONDS` | How long a request is held before it expires (default `120`) |
| `--no-color` | Plain output. Colour is also off when output isn't a terminal or `NO_COLOR` is set |

## Using the CLI

| Command | What it does |
|---|---|
| `classes` | List the address classes, their root networks and how full they are |
| `map [class \| network]` | Draw allocations as a grid. `map 10.4.0.0/20` zooms in on one block |
| `list [class]` | Table of allocations and pending leases, with time left to confirm |
| `request [class] [size] [assignee]` | Propose the nearest free block and hold it as a lease |
| `confirm <network>` | Confirm a pending lease before it expires |
| `release <network>` | Release an allocation, or drop a pending lease, so the space can be reused |
| `help [command]` | List commands, or show help for one |
| `quit` | Leave (all state is lost) |

A class can be given by name (`nonpci-spoke-prod`) or by its number from `classes` (`4`). Sizes can
be written `/24` or `24`. Run `request` with no arguments to be prompted for the class and size.
Tab completes class names and networks.

After `request`, you are asked whether to confirm:

- `y` confirms the allocation now.
- `n` drops the lease and frees the space.
- `l`, or just Enter, leaves it pending. Run `confirm <network>` later, before it expires.

Any lease that expires is reported before the next command runs.

### Example session

```text
$ python src/main.py --demo
cidr> request nonpci-spoke-prod /24 web

Proposed 10.4.7.0/24 for web in nonpci-spoke-prod (256 addresses).
It is held for 2:00 (until 19:31:01). Confirm it before then or it is released.

10.4.0.0/16  nonpci-spoke-prod  2.8% used  (1 cell = /24, 256 addresses)
  10.4.0.0        █████▒█▓························································
  10.4.64.0       ································································
  10.4.128.0      ································································
  10.4.192.0      ································································
  █ allocated  ▓ pending lease  ▒ partly used  · free

Confirm now? [y]es / [n]o / [l]ater (default: later): y
Confirmed 10.4.7.0/24 for web.

cidr> release 10.4.4.0/24
Released 10.4.4.0/24 (was search). It can be allocated again.

cidr> request 4 /24 reuse
Proposed 10.4.4.0/24 for reuse in nonpci-spoke-prod (256 addresses).
...
Confirm now? [y]es / [n]o / [l]ater (default: later): l
Left pending. Run 'confirm 10.4.4.0/24' within 2:00.

cidr> map 10.4.0.0/20
10.4.0.0/20  nonpci-spoke-prod  45.3% used  (1 cell = /28, 16 addresses)
  10.4.0.0        ████████████████████████████████████████████████████████████████
  10.4.4.0        ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓████············████████████████████████████████
  10.4.8.0        ································································
  10.4.12.0       ································································
```

Each map draws at most 256 cells, so a `/16` is shown as `/24` cells and a `/20` as `/28` cells.
A cell is `▒` when only part of it is used.

### Demo walkthrough

A short script for showing the allocator to someone. Start with
`python src/main.py --demo --lease-ttl 30` so you don't have to wait two minutes for a lease to expire.

1. **Nearest placement.** `map 4` shows the example allocations packed at the start of
   `10.4.0.0/16`. Run `request 4 /24` and confirm it with `y`: it lands right after them, not
   somewhere random.
2. **Filling gaps.** `request 4 /26` lands in the free half of the partly used `10.4.5.0/24`
   cell, next to the existing `/26`.
3. **Lease expiry.** `request 4 /24` and answer `l`. `list` shows it as pending with a countdown.
   Wait 30 seconds and run `list` again: the expiry is reported and the block is gone.
4. **Release and reuse.** `release 10.4.4.0/24`, then `request 4 /24`: you get `10.4.4.0/24` back,
   because it is now the closest free block. Do this after the step 3 lease has expired. If it's
   still pending, `10.4.9.0/24` (next to it) is equally close and may be chosen instead.
5. **Zoom.** `map 10.4.0.0/20` shows the same area at `/28` resolution.
6. **Separate classes.** `request pci-hub-prod /24` allocates from `10.1.0.0/16`. Each class only
   ever uses its own roots.

## How allocation works

### Classes and roots

Every allocation belongs to a **class** (a `Classifier`: PCI or not, hub or spoke, environment).
Each class has its own root networks, defined in `Store.__init__` in
[src/store/store.py](src/store/store.py):

| Class | Roots |
|---|---|
| `pci-hub-prod` | `10.1.0.0/16` |
| `pci-spoke-prod` | `10.2.0.0/16` |
| `nonpci-hub-prod` | `10.3.0.0/16` |
| `nonpci-spoke-prod` | `10.4.0.0/16`, `10.6.0.0/16`, `100.64.0.0/16` |

### Choosing a block

`find_available_network` in [src/cidr/cidr.py](src/cidr/cidr.py) searches outward from existing
allocations, one level at a time:

1. For a `/24` request, look at the `/23` around each existing allocation. That is the block's
   "buddy": the other half of the `/23`. If any `/24` inside it is free, use it.
2. If not, widen to the `/22` around each allocation, then the `/21`, and so on up to the root.
3. The first free block found is the closest one to something already allocated.
4. If nothing is free near an existing allocation (for example, the root is empty), take the
   first free block, checking roots in order. A full root spills over into the next one.

Example: with `10.4.0.0/22` and `10.4.5.0/26` allocated, a `/26` request gets `10.4.5.64/26`
(the `/26`'s buddy), and a `/24` request gets `10.4.4.0/24` (inside the `/23` around the `/26`).

Only allocations smaller than the block being searched can leave space in it, so larger ones are
skipped at each level. Requests larger than every root are rejected.

### What counts as taken

A network is taken if it overlaps a confirmed allocation that hasn't been released, or a pending
lease that hasn't expired. Start times are deliberately ignored here: if the system clock steps
backwards, a just-made allocation could otherwise look like it hadn't started yet, and its space
would be handed out twice.

### Lease lifecycle

```text
request ──> pending lease ──confirm (before deadline)──> allocated ──release──> free
                 │
                 ├──expires (deadline passes)──> free
                 └──declined / released──────> free
```

- `Store.obtain_temporary_lease` holds the block and sets `expires_at` to now + the lease TTL.
- `Store.commit_temporary_lease` turns it into an allocation, or raises `LeaseExpiredError` if
  the deadline has passed.
- Expired leases stop blocking space as soon as the deadline passes.
  `Store.expire_temporary_leases` removes them and returns them so they can be reported.

## Project layout

```text
src/
  main.py           interactive CLI (CidrShell)
  cidr/cidr.py      allocation algorithm: find_available_network, plus helpers
  store/store.py    in-memory store: roots, allocations, temporary leases
tests/
  test_cidr.py      placement, exhaustion, leases, clock skew, randomised stress test
  test_store.py     store behaviour, point-in-time queries, lease expiry
  test_cli.py       CLI commands driven with scripted input and a fake clock
```

## Running the tests

```bash
pytest
```

The suite takes about 30 seconds, almost all of it the randomised stress test in `test_cidr.py`.
Tests that involve lease expiry use a fake clock (`tests/conftest.py`), so they don't wait in
real time.

## Limitations

- **In memory, single process.** Nothing is persisted, and there is no locking for concurrent
  callers. The store does refuse a lease or allocation that overlaps an existing one, so a race
  fails with an error rather than allocating twice.
- **Hard-coded roots.** Classes and root networks are defined in code.
- **IPv4 only.**
- **Nearest, not best fit.** New blocks go next to existing ones, which keeps space contiguous,
  but the allocator doesn't look for the tightest-fitting gap. Heavy churn of mixed sizes could
  still fragment space.
- **Simple scans.** Lookups are linear over all allocations. That is fine for thousands of
  allocations, but would need an index for much more.
