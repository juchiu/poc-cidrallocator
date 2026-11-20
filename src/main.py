"""Interactive demo of the CIDR allocator.

Run from the repo root:

    python src/main.py            # empty store
    python src/main.py --demo     # start with a few example allocations

All state is in memory and is lost when you quit.
"""
import argparse
import cmd
import datetime
import ipaddress
import os
import sys

from cidr.cidr import find_available_network, get_classifier_from_ip, seed_demo
from store.store import LEASE_TTL_SECONDS, Allocation, Classifier, LeaseExpiredError, Store

MAP_COLUMNS = 64
# A map draws at most 2**MAP_ZOOM_BITS cells, so a /16 is drawn as /24 cells.
MAP_ZOOM_BITS = 8

HELP_TEXT = """\
Commands
  classes                        list address classes and their root networks
  map [class | network]          draw allocations as a grid, e.g. 'map 10.4.0.0/20' to zoom in
  list [class]                   table of allocations and pending leases
  request [class] [size] [who]   propose the nearest free block, then confirm it
  confirm <network>              confirm a pending lease before it expires
  release <network>              release an allocation (or drop a pending lease)
  quit                           leave the demo (all state is in memory)

A class can be given by name or by its number from 'classes'. Sizes look like /24 or 24.
Run 'request' with no arguments to be prompted for each value."""


class Palette:
    """ANSI colours, or plain text when disabled."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def bold(self, text: str) -> str:
        return self._wrap("1", text)


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def overlap_size(a: ipaddress.IPv4Network, b: ipaddress.IPv4Network) -> int:
    """Number of addresses two CIDR blocks share. CIDR blocks either nest or don't overlap."""
    if a.subnet_of(b):
        return a.num_addresses
    if b.subnet_of(a):
        return b.num_addresses
    return 0


def parse_prefix(text: str) -> int:
    value = text.strip().lstrip("/")
    if not value.isdigit() or not 1 <= int(value) <= 32:
        raise ValueError(f"Subnet size must be /1 to /32, got {text!r}")
    return int(value)


def parse_network(text: str) -> ipaddress.IPv4Network:
    if not text.strip():
        raise ValueError("Give a network, e.g. 10.4.1.0/24")
    return ipaddress.IPv4Network(text.strip())


class CidrShell(cmd.Cmd):
    prompt = "cidr> "

    def __init__(self, store: Store, palette: Palette | None = None, stdin=None, stdout=None):
        super().__init__(stdin=stdin, stdout=stdout)
        if stdin is not None:
            self.use_rawinput = False
        self.store = store
        self.c = palette or Palette(enabled=False)
        self.classifiers: list[Classifier] = list(store.roots)
        self.intro = self._intro()

    # --- output and input helpers ---

    def say(self, text: str = ""):
        self.stdout.write(text + "\n")

    def error(self, text: str):
        self.say(self.c.red(f"Error: {text}"))

    def ask(self, question: str) -> str | None:
        """Read one line of input; None on end of input."""
        if self.use_rawinput:
            try:
                return input(question)
            except EOFError:
                return None
        self.stdout.write(question)
        self.stdout.flush()
        line = self.stdin.readline()
        return line.rstrip("\n") if line else None

    def _intro(self) -> str:
        ttl = format_duration(self.store.lease_ttl_seconds)
        return "\n".join([
            self.c.bold("CIDR allocator demo"),
            f"Requests are held as a pending lease for {ttl} and expire unless confirmed.",
            "Type 'help' for commands. Try: request nonpci-spoke-prod /24",
            "",
        ])

    # --- cmd.Cmd hooks ---

    def preloop(self):
        # cmd.Cmd prints self.intro after preloop; print it here so the classes table follows it.
        if self.intro:
            self.say(self.intro)
            self.intro = None
            self.do_classes("")
            self.say()

    def precmd(self, line: str) -> str:
        for lease in self.store.expire_temporary_leases():
            self.say(self.c.yellow(
                f"Expired: the lease on {lease.network} for {lease.assignee} was not confirmed "
                f"within {format_duration(self.store.lease_ttl_seconds)}, so the space is free again."
            ))
        return line

    def onecmd(self, line: str) -> bool:
        try:
            return super().onecmd(line)
        except ValueError as e:
            self.error(str(e))
            return False

    def emptyline(self) -> bool:
        return False  # the default repeats the last command

    def default(self, line: str):
        self.error(f"Unknown command {line.split()[0]!r}. Type 'help' for the list.")

    # --- lookups ---

    def resolve_classifier(self, text: str) -> Classifier:
        text = text.strip()
        if text.isdigit() and 1 <= int(text) <= len(self.classifiers):
            return self.classifiers[int(text) - 1]
        for classifier in self.classifiers:
            if classifier.name == text:
                return classifier
        raise ValueError(f"Unknown class {text!r}. Run 'classes' to see the options.")

    def classifier_for(self, network: ipaddress.IPv4Network) -> Classifier:
        try:
            return get_classifier_from_ip(network, self.store.roots)
        except ValueError:
            raise ValueError(f"{network} is not inside any root network. Run 'classes' to see them.") from None

    def seconds_left(self, lease: Allocation) -> int:
        assert lease.expires_at is not None, "only pending leases have a deadline"
        return lease.expires_at - self.store.clock()

    # --- rendering ---

    def cell_glyph(self, cell: ipaddress.IPv4Network, allocations: list[Allocation]) -> str:
        touching = [a for a in allocations if a.network.overlaps(cell)]
        if not touching:
            return self.c.dim("·")
        pending = any(a.is_pending_lease for a in touching)
        used = sum(overlap_size(a.network, cell) for a in touching)
        colour = self.c.yellow if pending else self.c.green
        if used < cell.num_addresses:
            return colour("▒")
        return colour("▓" if pending else "█")

    def render_block(self, classifier: Classifier, block: ipaddress.IPv4Network):
        allocations = [a for a in self.store.get_active_allocations(classifier) if a.network.overlaps(block)]
        used = sum(overlap_size(a.network, block) for a in allocations)
        cell_prefix = min(block.prefixlen + MAP_ZOOM_BITS, 32)
        cells = list(block.subnets(new_prefix=cell_prefix))

        self.say(
            f"{self.c.bold(str(block))}  {classifier.name}  "
            f"{used / block.num_addresses:.1%} used  "
            + self.c.dim(f"(1 cell = /{cell_prefix}, {cells[0].num_addresses} addresses)")
        )
        for start in range(0, len(cells), MAP_COLUMNS):
            row = cells[start:start + MAP_COLUMNS]
            label = str(row[0].network_address).ljust(15)
            self.say(f"  {self.c.dim(label)} " + "".join(self.cell_glyph(cell, allocations) for cell in row))

    def legend(self):
        self.say(
            f"  {self.c.green('█')} allocated  {self.c.yellow('▓')} pending lease  "
            f"{self.c.green('▒')} partly used  {self.c.dim('·')} free"
        )

    def show_root_of(self, classifier: Classifier, network: ipaddress.IPv4Network):
        root = next(r for r in self.store.get_roots(classifier) if network.subnet_of(r))
        self.render_block(classifier, root)
        self.legend()

    # --- commands ---

    def do_help(self, arg: str):
        """help [command] - list commands, or show help for one."""
        if arg:
            return super().do_help(arg)
        self.say(HELP_TEXT)

    def do_classes(self, arg: str):
        """classes - list address classes (classifiers), their root networks and how full they are."""
        self.say(self.c.bold(f"  #  {'class':<20}{'roots':<46}{'used':>7}"))
        for number, classifier in enumerate(self.classifiers, start=1):
            roots = self.store.get_roots(classifier)
            allocations = self.store.get_active_allocations(classifier)
            total = sum(root.num_addresses for root in roots)
            used = sum(a.network.num_addresses for a in allocations)
            pending = sum(1 for a in allocations if a.is_pending_lease)
            note = self.c.yellow(f"  {pending} pending") if pending else ""
            root_list = ", ".join(str(root) for root in roots)
            self.say(f"  {number}  {classifier.name:<20}{root_list:<46}{used / total:>7.1%}{note}")

    def do_map(self, arg: str):
        """map [class | network] - draw allocations as a grid.

        With no argument, draws every root network. With a class, draws that class's roots.
        With a network, zooms in on it, e.g. 'map 10.4.0.0/20' shows /28 cells."""
        arg = arg.strip()
        if not arg:
            targets = [(c, root) for c in self.classifiers for root in self.store.get_roots(c)]
        else:
            try:
                classifier = self.resolve_classifier(arg)
                targets = [(classifier, root) for root in self.store.get_roots(classifier)]
            except ValueError:
                network = parse_network(arg)
                targets = [(self.classifier_for(network), network)]
        for classifier, block in targets:
            self.render_block(classifier, block)
            self.say()
        self.legend()

    def do_list(self, arg: str):
        """list [class] - table of active allocations and pending leases."""
        classifiers = [self.resolve_classifier(arg)] if arg.strip() else self.classifiers
        rows = [
            allocation
            for classifier in classifiers
            for allocation in sorted(self.store.get_active_allocations(classifier), key=lambda a: a.network)
        ]
        if not rows:
            self.say("No allocations yet. Try 'request'.")
            return
        self.say(self.c.bold(f"  {'network':<20}{'class':<20}{'addresses':>10}  {'assignee':<16}status"))
        for a in rows:
            if a.is_pending_lease:
                status = self.c.yellow(f"pending, {format_duration(self.seconds_left(a))} left to confirm")
            else:
                status = self.c.green("allocated")
            self.say(f"  {a.network!s:<20}{a.classifier.name:<20}{a.network.num_addresses:>10}  {a.assignee:<16}{status}")

    def do_request(self, arg: str):
        """request [class] [size] [assignee] - propose the nearest free block and hold it as a lease.

        Missing class or size are prompted for. You can confirm straight away, drop it, or leave it
        pending and run 'confirm <network>' later, as long as it's before the lease expires."""
        tokens = arg.split()
        if tokens:
            classifier = self.resolve_classifier(tokens[0])
        else:
            for number, c in enumerate(self.classifiers, start=1):
                self.say(f"  {number}  {c.name}")
            answer = self.ask("Class (name or number): ")
            if answer is None:
                return
            classifier = self.resolve_classifier(answer)

        if len(tokens) > 1:
            prefix = parse_prefix(tokens[1])
        else:
            answer = self.ask("Subnet size (e.g. /24): ")
            if answer is None:
                return
            prefix = parse_prefix(answer)

        assignee = tokens[2] if len(tokens) > 2 else "demo"

        network = find_available_network(self.store, classifier, prefix)
        lease = self.store.obtain_temporary_lease(classifier, network, assignee)
        deadline = (datetime.datetime.now() + datetime.timedelta(seconds=self.seconds_left(lease))).strftime("%H:%M:%S")
        self.say()
        self.say(f"Proposed {self.c.bold(str(network))} for {assignee} in {classifier.name} "
                 f"({network.num_addresses} addresses).")
        self.say(self.c.yellow(
            f"It is held for {format_duration(self.store.lease_ttl_seconds)} (until {deadline}). "
            f"Confirm it before then or it is released."
        ))
        self.say()
        self.show_root_of(classifier, network)
        self.say()

        answer = self.ask("Confirm now? [y]es / [n]o / [l]ater (default: later): ")
        answer = (answer or "").strip().lower()
        if answer in ("y", "yes"):
            self.confirm_lease(lease)
        elif answer in ("n", "no"):
            self.store.cancel_temporary_lease(lease)
            self.say(f"Dropped the lease; {network} is free again.")
        else:
            self.say(f"Left pending. Run 'confirm {network}' within "
                     f"{format_duration(self.seconds_left(lease))}.")

    def confirm_lease(self, lease: Allocation):
        try:
            self.store.commit_temporary_lease(lease)
        except LeaseExpiredError:
            self.error(f"Too late: the lease on {lease.network} expired before it was confirmed. "
                       f"Run 'request' again.")
            return
        self.say(self.c.green(f"Confirmed {lease.network} for {lease.assignee}."))

    def do_confirm(self, arg: str):
        """confirm <network> - confirm a pending lease before it expires."""
        network = parse_network(arg)
        classifier = self.classifier_for(network)
        lease = self.store.get_temporary_lease(classifier, network)
        if lease is None:
            if self.store.get_allocation(classifier, network):
                raise ValueError(f"{network} is already confirmed")
            raise ValueError(f"No pending lease for {network}. It may have expired; run 'list' to check.")
        self.confirm_lease(lease)

    def do_release(self, arg: str):
        """release <network> - release an allocation so it can be handed out again.

        Also drops a pending lease for that network."""
        network = parse_network(arg)
        classifier = self.classifier_for(network)
        lease = self.store.get_temporary_lease(classifier, network)
        if lease is not None:
            self.store.cancel_temporary_lease(lease)
            self.say(f"Dropped the pending lease on {network}; it is free again.")
            return
        allocation = self.store.release_allocation(classifier, network)
        self.say(f"Released {network} (was {allocation.assignee}). It can be allocated again.")

    def do_quit(self, arg: str) -> bool:
        """quit - leave the demo. All state is in memory and is lost."""
        return True

    do_exit = do_quit

    def do_EOF(self, arg: str) -> bool:
        self.say()
        return True

    # --- tab completion ---

    def _complete(self, text: str, options: list[str]) -> list[str]:
        return [option for option in options if option.startswith(text)]

    def complete_request(self, text, line, begidx, endidx):
        if len(line[:begidx].split()) == 1:
            return self._complete(text, [c.name for c in self.classifiers])
        return []

    complete_list = complete_request

    def complete_map(self, text, line, begidx, endidx):
        return self._complete(text, [c.name for c in self.classifiers])

    def complete_confirm(self, text, line, begidx, endidx):
        return self._complete(text, [
            str(lease.network) for leases in self.store.temporary_lease.values() for lease in leases
        ])

    def complete_release(self, text, line, begidx, endidx):
        return self._complete(text, [
            str(a.network) for c in self.classifiers for a in self.store.get_active_allocations(c)
        ])


def configure_readline():
    try:
        import readline
    except ImportError:  # e.g. Windows without pyreadline
        return
    # Class names contain '-' and networks contain '/', which readline would otherwise split on.
    readline.set_completer_delims(" \t\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive demo of the CIDR allocator.")
    parser.add_argument(
        "--lease-ttl", type=int, default=LEASE_TTL_SECONDS, metavar="SECONDS",
        help="how long a proposed allocation is held before it expires (default: %(default)s)",
    )
    parser.add_argument("--demo", action="store_true", help="start with a few example allocations")
    parser.add_argument("--no-color", action="store_true", help="disable coloured output")
    args = parser.parse_args(argv)
    if args.lease_ttl <= 0:
        parser.error("--lease-ttl must be positive")

    store = Store(lease_ttl_seconds=args.lease_ttl)
    if args.demo:
        seed_demo(store)

    colour = sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ
    shell = CidrShell(store, Palette(enabled=colour))
    configure_readline()
    while True:
        try:
            shell.cmdloop()
            return 0
        except KeyboardInterrupt:
            # Ctrl-C abandons the current command or prompt, not the whole demo.
            print("^C")


if __name__ == "__main__":
    sys.exit(main())
