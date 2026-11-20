import pytest


class FakeClock:
    """Stands in for Store's wall clock so tests can move time forward."""

    def __init__(self, now: int = 1_000_000):
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int):
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
