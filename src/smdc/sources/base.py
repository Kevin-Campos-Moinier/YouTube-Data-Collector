"""The contract every platform source implements.

Everything platform-specific (endpoints, payload shapes, error codes, target
syntax, cost model) lives behind this protocol. The pipeline, store, text
processing, exports and dashboard only ever see `Container`, `Item` and `Page`.

To add a platform: write `sources/<name>.py` with a class satisfying `Source`,
then register it in `sources/__init__.py`.
"""

from collections.abc import Iterator
from typing import Protocol

from ..config import Collect
from ..model import Container, Estimate, Page


class BudgetExhausted(Exception):
    """The platform refused a call because the budget/quota is used up."""


class Unavailable(Exception):
    """A container cannot be collected (comments disabled, deleted, forbidden...).

    Attributes:
        reason: Short machine-readable reason, stored as the container status.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Source(Protocol):
    """A platform adapter. `name` is also the ledger/budget key."""

    name: str

    def expand(self, targets: list[str], params: Collect) -> list[str]:
        """Turns user targets (IDs, URLs, channels, searches) into unique container IDs.

        Raises:
            ValueError: On a target the source cannot interpret.
        """

    def resolve(self, ids: list[str]) -> list[Container]:
        """Fetches container metadata, in input order; unknown IDs come back
        with availability "not_found"."""

    def estimate(self, containers: list[Container], params: Collect) -> Estimate:
        """Budget units the collection should cost, without calling the API."""

    def threads(self, container: Container, params: Collect,
                checkpoint: dict | None) -> Iterator[Page]:
        """Yields pages of top-level items (plus replies that come for free),
        each with the checkpoint to resume after it; honours `max_items`
        except in "oldest" order, where the pipeline selects afterwards.

        Raises:
            Unavailable: The container cannot be collected.
            BudgetExhausted: Quota is used up; resume later from the last checkpoint.
        """

    def replies(self, container_id: str, thread_id: str, limit: int) -> Page:
        """Returns up to `limit` replies of a thread, in platform order."""

    def exists(self, item_ids: list[str]) -> set[str]:
        """Returns the subset of item IDs still publicly visible."""
