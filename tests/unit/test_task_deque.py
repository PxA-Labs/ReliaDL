"""
Unit tests for the Chase-Lev work-stealing deque in src.algorithms.task_deque.
Verifies owner LIFO ordering, thief FIFO ordering, last-task hand-off between
the two ends, circular buffer growth, capacity validation, and — the property
the scheduler actually depends on — that no task is lost or duplicated under
sustained multi-threaded steal contention.
"""

from __future__ import annotations

import queue
import threading
import unittest
from typing import List

from src.algorithms.task_deque import (
    DEFAULT_INITIAL_CAPACITY,
    ChaseLevDeque,
    StealResult,
    StealStatus,
)
from src.exceptions import ConfigurationError


class TestConstruction(unittest.TestCase):
    """Capacity must be a positive power of two for mask addressing to work."""

    def test_default_capacity(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        self.assertEqual(deque.capacity, DEFAULT_INITIAL_CAPACITY)
        self.assertTrue(deque.is_empty)
        self.assertEqual(len(deque), 0)

    def test_custom_power_of_two_capacity_accepted(self) -> None:
        for capacity in (1, 2, 4, 256, 1024):
            self.assertEqual(ChaseLevDeque(capacity=capacity).capacity, capacity)

    def test_non_power_of_two_capacity_rejected(self) -> None:
        for capacity in (3, 5, 100, 1000):
            with self.assertRaises(ConfigurationError):
                ChaseLevDeque(capacity=capacity)

    def test_non_positive_capacity_rejected(self) -> None:
        for capacity in (0, -1, -64):
            with self.assertRaises(ConfigurationError):
                ChaseLevDeque(capacity=capacity)

    def test_non_integer_capacity_rejected(self) -> None:
        for capacity in (64.0, "64", None, True):
            with self.assertRaises(ConfigurationError):
                ChaseLevDeque(capacity=capacity)  # type: ignore[arg-type]


class TestOwnerOperations(unittest.TestCase):
    """The owner's end is a stack: last pushed is first popped."""

    def test_push_pop_is_lifo(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        for value in range(5):
            deque.push(value)
        self.assertEqual([deque.pop() for _ in range(5)], [4, 3, 2, 1, 0])

    def test_pop_on_empty_returns_none(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        self.assertIsNone(deque.pop())

    def test_repeated_pop_on_empty_does_not_corrupt_indices(self) -> None:
        """A pop on an empty deque must undo its speculative bottom decrement."""
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        for _ in range(10):
            self.assertIsNone(deque.pop())
        self.assertEqual(len(deque), 0)

        # The deque must still be usable after the failed pops.
        deque.push(42)
        self.assertEqual(len(deque), 1)
        self.assertEqual(deque.pop(), 42)

    def test_interleaved_push_and_pop(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        deque.push(1)
        deque.push(2)
        self.assertEqual(deque.pop(), 2)
        deque.push(3)
        self.assertEqual(deque.pop(), 3)
        self.assertEqual(deque.pop(), 1)
        self.assertIsNone(deque.pop())

    def test_pushing_none_is_rejected(self) -> None:
        """None marks a vacant slot internally and cannot be a task."""
        deque: ChaseLevDeque[object] = ChaseLevDeque()
        with self.assertRaises(ConfigurationError):
            deque.push(None)  # type: ignore[arg-type]

    def test_len_never_reports_negative(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        deque.pop()
        self.assertGreaterEqual(len(deque), 0)

    def test_drain_returns_newest_first(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        for value in range(4):
            deque.push(value)
        self.assertEqual(deque.drain(), [3, 2, 1, 0])
        self.assertTrue(deque.is_empty)


class TestStealOperations(unittest.TestCase):
    """The thief's end is a queue: oldest task leaves first."""

    def test_steal_is_fifo(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        for value in range(5):
            deque.push(value)
        stolen = [deque.steal().task for _ in range(5)]
        self.assertEqual(stolen, [0, 1, 2, 3, 4])

    def test_steal_from_empty_reports_empty(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        result = deque.steal()
        self.assertIs(result.status, StealStatus.EMPTY)
        self.assertIsNone(result.task)
        self.assertFalse(result.is_success)
        self.assertFalse(result.should_retry)

    def test_owner_and_thief_consume_opposite_ends(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        for value in range(4):
            deque.push(value)
        self.assertEqual(deque.pop(), 3)
        self.assertEqual(deque.steal().task, 0)
        self.assertEqual(deque.pop(), 2)
        self.assertEqual(deque.steal().task, 1)
        self.assertTrue(deque.is_empty)

    def test_steal_result_flags(self) -> None:
        success: StealResult[int] = StealResult(StealStatus.SUCCESS, 7)
        self.assertTrue(success.is_success)
        self.assertFalse(success.should_retry)

        aborted: StealResult[int] = StealResult(StealStatus.ABORTED)
        self.assertFalse(aborted.is_success)
        self.assertTrue(aborted.should_retry)

    def test_steal_result_is_immutable(self) -> None:
        result: StealResult[int] = StealResult(StealStatus.SUCCESS, 1)
        with self.assertRaises(Exception):
            result.task = 2  # type: ignore[misc]

    def test_try_steal_stops_on_empty(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        self.assertIs(deque.try_steal().status, StealStatus.EMPTY)

    def test_try_steal_returns_task_when_available(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        deque.push(9)
        self.assertEqual(deque.try_steal().task, 9)

    def test_try_steal_validates_attempts(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        for attempts in (0, -1, 1.5, "3", True):
            with self.assertRaises(ConfigurationError):
                deque.try_steal(attempts=attempts)  # type: ignore[arg-type]


class TestLastTaskHandoff(unittest.TestCase):
    """
    The single contested case: one task left, with both ends reaching for it.

    Exactly one of the owner and the thief may win, and the loser must come away
    empty rather than returning the same task twice.
    """

    def test_thief_wins_single_task_then_owner_sees_empty(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        deque.push(1)
        self.assertEqual(deque.steal().task, 1)
        self.assertIsNone(deque.pop())

    def test_owner_wins_single_task_then_thief_sees_empty(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        deque.push(1)
        self.assertEqual(deque.pop(), 1)
        self.assertIs(deque.steal().status, StealStatus.EMPTY)

    def test_two_thieves_cannot_both_take_the_last_task(self) -> None:
        """
        Drive the real race: both thieves read the same top index, then swap.

        The first steal is paused between its speculative read and its CAS while
        a second steal runs to completion, which is precisely the interleaving
        the compare-and-swap exists to resolve.
        """
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        deque.push(99)

        first_read = threading.Event()
        second_done = threading.Event()
        outcomes: List[StealResult[int]] = []

        original_lock = deque._cas_lock

        class PausingLock:
            """Blocks the first CAS until a competing steal has finished."""

            def __init__(self) -> None:
                self._tripped = False

            def __enter__(self):
                if not self._tripped:
                    self._tripped = True
                    first_read.set()
                    second_done.wait(timeout=5)
                return original_lock.__enter__()

            def __exit__(self, *args):
                return original_lock.__exit__(*args)

        deque._cas_lock = PausingLock()  # type: ignore[assignment]

        def slow_thief() -> None:
            outcomes.append(deque.steal())

        thread = threading.Thread(target=slow_thief)
        thread.start()
        self.assertTrue(first_read.wait(timeout=5))

        # The competing thief runs while the first is parked before its CAS.
        outcomes.append(deque.steal())
        second_done.set()
        thread.join(timeout=5)

        self.assertEqual(len(outcomes), 2)
        successes = [r for r in outcomes if r.is_success]
        aborts = [r for r in outcomes if r.should_retry]
        self.assertEqual(len(successes), 1, "the task must be handed out once")
        self.assertEqual(len(aborts), 1, "the loser must report contention")
        self.assertEqual(successes[0].task, 99)

    def test_owner_loses_the_last_task_when_a_thief_wins_first(self) -> None:
        """
        The mirror of the two-thief race: the owner must lose gracefully.

        Requires a three-step interleaving that cannot occur by chance under the
        GIL — the thief reads the last task, the owner then claims the bottom
        and reads the same top index, the thief's swap lands, and only then does
        the owner attempt its own. The owner must come away empty rather than
        handing out a task a thief already holds.
        """
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        deque.push(77)

        arrivals: "queue.Queue[threading.Event]" = queue.Queue()
        real_lock = deque._cas_lock

        class SequencedLock:
            """Parks each arriving swap until the test releases it by name."""

            def __enter__(self):
                gate = threading.Event()
                arrivals.put(gate)
                gate.wait(timeout=5)
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        deque._cas_lock = SequencedLock()  # type: ignore[assignment]

        stolen: List[StealResult[int]] = []
        popped: List[object] = []

        thief = threading.Thread(target=lambda: stolen.append(deque.steal()))
        thief.start()
        # The thief has finished its speculative read and is parked at the swap.
        thief_gate = arrivals.get(timeout=5)

        owner = threading.Thread(target=lambda: popped.append(deque.pop()))
        owner.start()
        # The owner has claimed the bottom and read the contested top index.
        owner_gate = arrivals.get(timeout=5)

        thief_gate.set()
        thief.join(timeout=5)
        owner_gate.set()
        owner.join(timeout=5)

        self.assertEqual(len(stolen), 1)
        self.assertEqual(len(popped), 1)
        self.assertTrue(stolen[0].is_success, "the thief swapped first and must win")
        self.assertEqual(stolen[0].task, 77)
        self.assertIsNone(popped[0], "the owner must not re-issue a stolen task")

    def test_task_is_invisible_to_thieves_until_fully_stored(self) -> None:
        """
        A pushed task becomes visible only after its slot is written.

        If the owner published the new bottom before storing the task, a thief
        would read a vacant slot and hand the scheduler a task that is None.
        Blocking inside the store makes that window observable.
        """
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=4)
        storing = threading.Event()
        release = threading.Event()
        backing = deque._array

        class BlockingArray:
            """Stalls inside the slot write, holding the publish window open."""

            capacity = backing.capacity

            def get(self, index: int):
                return backing.get(index)

            def put(self, index: int, task) -> None:
                storing.set()
                release.wait(timeout=5)
                backing.put(index, task)

            def grow(self, top: int, bottom: int):
                return backing.grow(top, bottom)

        deque._array = BlockingArray()  # type: ignore[assignment]

        pusher = threading.Thread(target=lambda: deque.push(5))
        pusher.start()
        self.assertTrue(storing.wait(timeout=5), "push never reached its store")

        result = deque.steal()

        release.set()
        pusher.join(timeout=5)

        self.assertFalse(
            result.is_success and result.task is None,
            "a half-published slot was handed out as a task",
        )
        self.assertIs(
            result.status,
            StealStatus.EMPTY,
            "an unpublished task must not be visible to thieves",
        )

    def test_try_steal_retries_after_abort(self) -> None:
        """An aborted attempt is retried; a subsequent task is then returned."""
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        deque.push(1)
        deque.push(2)

        calls: List[int] = []
        real_steal = deque.steal

        def flaky_steal() -> StealResult[int]:
            calls.append(1)
            if len(calls) == 1:
                return StealResult(StealStatus.ABORTED)
            return real_steal()

        deque.steal = flaky_steal  # type: ignore[assignment]
        result = deque.try_steal(attempts=3)
        self.assertTrue(result.is_success)
        self.assertEqual(result.task, 1)
        self.assertEqual(len(calls), 2)

    def test_try_steal_gives_up_after_exhausting_attempts(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque()
        deque.steal = lambda: StealResult(StealStatus.ABORTED)  # type: ignore[assignment]
        result = deque.try_steal(attempts=4)
        self.assertIs(result.status, StealStatus.ABORTED)


class TestCircularBufferGrowth(unittest.TestCase):
    """Growth must preserve every queued task and both consumption orders."""

    def test_growth_preserves_lifo_order(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=2)
        for value in range(100):
            deque.push(value)
        self.assertGreaterEqual(deque.capacity, 128)
        self.assertEqual(len(deque), 100)
        self.assertEqual(deque.drain(), list(reversed(range(100))))

    def test_growth_preserves_fifo_order_for_thieves(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=1)
        for value in range(50):
            deque.push(value)
        stolen = [deque.steal().task for _ in range(50)]
        self.assertEqual(stolen, list(range(50)))

    def test_growth_carries_only_the_live_window(self) -> None:
        """Tasks already consumed must not reappear when the buffer doubles."""
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=2)
        deque.push(1)
        deque.push(2)
        self.assertEqual(deque.steal().task, 1)
        for value in range(3, 20):
            deque.push(value)
        remaining = sorted(deque.drain())
        self.assertEqual(remaining, [2] + list(range(3, 20)))
        self.assertNotIn(1, remaining)

    def test_capacity_doubles_rather_than_blocking(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=4)
        for value in range(5):
            deque.push(value)
        self.assertEqual(deque.capacity, 8)

    def test_wraparound_addressing_after_many_cycles(self) -> None:
        """
        Indices grow monotonically forever; only the mask wraps.

        Cycling far past the capacity exercises the ring addressing that would
        otherwise only be hit on a long-running transfer.
        """
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=4)
        for value in range(1000):
            deque.push(value)
            self.assertEqual(deque.steal().task, value)
        self.assertTrue(deque.is_empty)
        self.assertEqual(deque.capacity, 4, "steady state must not force growth")


class TestConcurrentContention(unittest.TestCase):
    """
    The acceptance criterion: zero task loss under concurrent steals.

    Every task pushed must be claimed by exactly one party. A duplicate means
    two workers would download the same byte range; a loss means a range is
    never downloaded and the transfer hangs at completion.
    """

    def _run_contention(
        self, task_count: int, thief_count: int, owner_pops: bool
    ) -> List[int]:
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=8)
        for value in range(task_count):
            deque.push(value)

        claimed: List[int] = []
        guard = threading.Lock()
        start = threading.Barrier(thief_count + (1 if owner_pops else 0))

        def thief() -> None:
            local: List[int] = []
            start.wait(timeout=10)
            misses = 0
            while misses < 50:
                result = deque.steal()
                if result.is_success:
                    local.append(result.task)  # type: ignore[arg-type]
                    misses = 0
                elif result.status is StealStatus.EMPTY:
                    misses += 1
            with guard:
                claimed.extend(local)

        def owner() -> None:
            local: List[int] = []
            start.wait(timeout=10)
            misses = 0
            while misses < 50:
                task = deque.pop()
                if task is None:
                    misses += 1
                else:
                    local.append(task)
                    misses = 0
            with guard:
                claimed.extend(local)

        threads = [threading.Thread(target=thief) for _ in range(thief_count)]
        if owner_pops:
            threads.append(threading.Thread(target=owner))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        for thread in threads:
            self.assertFalse(thread.is_alive(), "contention run did not terminate")
        return claimed

    def test_no_task_lost_or_duplicated_among_thieves(self) -> None:
        claimed = self._run_contention(task_count=5000, thief_count=6, owner_pops=False)
        self.assertEqual(len(claimed), 5000, "task count changed under contention")
        self.assertEqual(sorted(claimed), list(range(5000)), "tasks lost or duplicated")

    def test_no_task_lost_or_duplicated_with_owner_popping(self) -> None:
        """The hardest case: both ends consuming, meeting at the last task."""
        claimed = self._run_contention(task_count=5000, thief_count=4, owner_pops=True)
        self.assertEqual(len(claimed), 5000)
        self.assertEqual(sorted(claimed), list(range(5000)))

    def test_concurrent_push_and_steal_loses_nothing(self) -> None:
        """The owner produces while thieves consume, forcing growth mid-flight."""
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=2)
        total = 4000
        claimed: List[int] = []
        guard = threading.Lock()
        producing = threading.Event()
        producing.set()

        def thief() -> None:
            local: List[int] = []
            while producing.is_set() or not deque.is_empty:
                result = deque.steal()
                if result.is_success:
                    local.append(result.task)  # type: ignore[arg-type]
            with guard:
                claimed.extend(local)

        thieves = [threading.Thread(target=thief) for _ in range(4)]
        for thread in thieves:
            thread.start()
        for value in range(total):
            deque.push(value)
        producing.clear()
        for thread in thieves:
            thread.join(timeout=30)

        claimed.extend(deque.drain())
        self.assertEqual(sorted(claimed), list(range(total)))

    def test_deque_is_empty_after_full_contention(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=8)
        for value in range(200):
            deque.push(value)

        def thief() -> None:
            while deque.steal().status is not StealStatus.EMPTY:
                pass

        threads = [threading.Thread(target=thief) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertTrue(deque.is_empty)
        self.assertIsNone(deque.pop())


class TestRepresentation(unittest.TestCase):
    """The repr is used in scheduler debug logs and must show both ends."""

    def test_repr_reports_size_and_capacity(self) -> None:
        deque: ChaseLevDeque[int] = ChaseLevDeque(capacity=4)
        deque.push(1)
        text = repr(deque)
        self.assertIn("size=1", text)
        self.assertIn("capacity=4", text)
        self.assertIn("top=", text)
        self.assertIn("bottom=", text)


if __name__ == "__main__":
    unittest.main()
