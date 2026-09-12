"""
Chase-Lev work-stealing double-ended task deque for ReliaDL.

Each download worker owns one deque of pending byte-range tasks. The owner
pushes and pops at the *bottom*; idle workers steal from the *top*. Separating
the two ends is what makes work stealing cheap: the owner's fast path never
touches the same memory the thieves contend on, so the common case (a busy
worker consuming its own queue) costs no synchronization at all.

The owner consumes LIFO and thieves steal FIFO, which is deliberate rather than
incidental. The owner's most recently pushed task is the one whose neighbouring
ranges are still warm in the connection and page cache, while the oldest task at
the far end is the coldest and therefore the cheapest to hand to another worker.
The two ends only meet when a deque is down to its last task, which is the sole
case requiring an atomic hand-off.

    push/pop (owner, LIFO)                    steal (thieves, FIFO)
              |                                         |
              v                                         v
         +----------------------------------------------+
         | bottom -->                        <-- top    |
         +----------------------------------------------+

Atomicity in Python
-------------------
Chase-Lev is a lock-free algorithm built on an atomic compare-and-swap of the
top index. CPython exposes no CAS primitive, and the GIL guarantees atomicity
only for individual bytecodes, not for the read-compare-write sequence a CAS
requires. This implementation therefore keeps the algorithm and its invariants
exactly, and realizes the single CAS with a short critical section. The
structure is preserved where it matters: ``push`` never locks, ``pop`` locks
only when taking the last task, and ``steal`` holds the lock for the compare
and swap alone, not for the buffer read that precedes it.

Calling it lock-free would be false. What it does keep is the property that
actually buys throughput here, namely that an uncontended worker draining its
own deque acquires nothing.

Two races survive that treatment and are safe by construction rather than by
locking. A thief may read ``self._array`` while the owner is growing it, but
``grow`` copies the entire live window ``[top, bottom)`` into the larger buffer
and the owner only publishes a task by incrementing ``bottom`` *after* writing
it, so every index a thief is entitled to read holds the same task in both the
old and the new array. A thief may also read a task and then lose the CAS; that
is the ``ABORTED`` outcome, and the task is untouched and still owned by
whoever won.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Generic, List, Optional, TypeVar

from src.algorithms.adaptive_chunker import is_power_of_two
from src.exceptions import ConfigurationError

T = TypeVar("T")

# Initial slot count of a worker's circular buffer. Sized so a typical transfer
# never grows it: the array doubles rather than blocking, but growth copies the
# live window and is worth avoiding on the owner's fast path.
DEFAULT_INITIAL_CAPACITY = 64

# Bound on retries in try_steal(). An aborted steal means a competing thief or
# the owner won the task, so retrying is worthwhile, but only briefly: under
# heavy contention a thief is better off trying a different victim deque.
DEFAULT_STEAL_ATTEMPTS = 3


class StealStatus(str, Enum):
    """Outcome of a single attempt to steal from a deque."""

    # A task was removed from the top of the victim deque.
    SUCCESS = "SUCCESS"

    # The victim deque held no stealable task at the observed instant.
    EMPTY = "EMPTY"

    # Another thief or the owner won the contested task; the caller may retry.
    ABORTED = "ABORTED"


@dataclass(frozen=True)
class StealResult(Generic[T]):
    """
    Result of a steal attempt, distinguishing "nothing there" from "lost a race".

    The distinction drives thief policy and must not be collapsed into an
    Optional: ``EMPTY`` means look for another victim, while ``ABORTED`` means
    this victim had work a moment ago and retrying the same deque is sensible.

    Attributes:
        status: Which of the three outcomes occurred.
        task: The stolen task, present only when the status is SUCCESS.
    """

    status: StealStatus
    task: Optional[T] = None

    @property
    def is_success(self) -> bool:
        """True when a task was actually removed from the victim."""
        return self.status is StealStatus.SUCCESS

    @property
    def should_retry(self) -> bool:
        """True when the attempt lost a race and the victim may still hold work."""
        return self.status is StealStatus.ABORTED


# Shared immutable results for the two task-less outcomes, which carry no
# payload and are returned on hot paths.
_EMPTY_RESULT: StealResult = StealResult(StealStatus.EMPTY)
_ABORTED_RESULT: StealResult = StealResult(StealStatus.ABORTED)


class _CircularArray(Generic[T]):
    """
    Power-of-two ring buffer addressed by monotonically increasing indices.

    Indices are never wrapped by the caller; the mask does it here. Keeping
    ``top`` and ``bottom`` strictly monotonic is what lets the CAS compare bare
    integers without an ABA counter, since an index value is never reused.
    """

    __slots__ = ("_capacity", "_mask", "_buffer")

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._mask = capacity - 1
        self._buffer: List[Optional[T]] = [None] * capacity

    @property
    def capacity(self) -> int:
        """Number of slots in the ring."""
        return self._capacity

    def get(self, index: int) -> Optional[T]:
        """Read the task stored at a monotonic index."""
        return self._buffer[index & self._mask]

    def put(self, index: int, task: Optional[T]) -> None:
        """Store a task at a monotonic index."""
        self._buffer[index & self._mask] = task

    def grow(self, top: int, bottom: int) -> "_CircularArray[T]":
        """
        Return a double-width copy holding the live window [top, bottom).

        The old array is left intact. A thief mid-steal may still be holding a
        reference to it, and every index in the live window resolves to the same
        task in both arrays, so that thief reads correct data either way.
        """
        larger: _CircularArray[T] = _CircularArray(self._capacity * 2)
        for index in range(top, bottom):
            larger.put(index, self.get(index))
        return larger


class ChaseLevDeque(Generic[T]):
    """
    Single-owner, multi-thief work-stealing deque.

    Exactly one thread or coroutine may call ``push`` and ``pop`` — they are the
    owner's operations and assume no competing writer at the bottom end. Any
    number of threads may call ``steal`` concurrently.

    Violating single ownership corrupts the deque rather than raising, which is
    inherent to the algorithm: the owner's fast path is cheap precisely because
    it does not defend against a second owner.
    """

    def __init__(self, capacity: int = DEFAULT_INITIAL_CAPACITY) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise ConfigurationError(
                f"capacity must be an integer, got {type(capacity).__name__}",
                parameter="capacity",
                value=capacity,
            )
        if capacity < 1:
            raise ConfigurationError(
                f"capacity must be at least 1, got {capacity}",
                parameter="capacity",
                value=capacity,
            )
        if not is_power_of_two(capacity):
            raise ConfigurationError(
                f"capacity must be a power of two for mask addressing, got {capacity}",
                parameter="capacity",
                value=capacity,
            )

        self._array: _CircularArray[T] = _CircularArray(capacity)
        self._top: int = 0
        self._bottom: int = 0

        # Guards the compare-and-swap on _top, standing in for the hardware CAS
        # the algorithm assumes. Held for the comparison and the store only.
        self._cas_lock = threading.Lock()

    @property
    def capacity(self) -> int:
        """Current slot count of the backing ring buffer."""
        return self._array.capacity

    @property
    def is_empty(self) -> bool:
        """True when no task is currently queued."""
        return len(self) == 0

    def __len__(self) -> int:
        """
        Number of queued tasks.

        Observational only. Under concurrent stealing the value may be stale by
        the time it is read, and ``pop`` transiently drives ``bottom`` below
        ``top``, so the count is floored at zero rather than reported negative.
        """
        return max(0, self._bottom - self._top)

    def push(self, task: T) -> None:
        """
        Append a task at the bottom. Owner only.

        Never blocks and never locks: the buffer is grown rather than waiting
        for thieves to drain it, so a producing worker is never stalled by
        contention at the far end.
        """
        if task is None:
            raise ConfigurationError(
                "Cannot push None onto a task deque; None marks an empty slot",
                parameter="task",
                value=task,
            )

        bottom = self._bottom
        top = self._top
        if bottom - top >= self._array.capacity:
            self._array = self._array.grow(top, bottom)

        self._array.put(bottom, task)
        # Publishing step: a task becomes visible to thieves only once bottom
        # passes it, so the store above is always complete before it is read.
        self._bottom = bottom + 1

    def pop(self) -> Optional[T]:
        """
        Remove and return the most recently pushed task, or None if empty. Owner only.

        Claims the bottom slot before reading ``top``, so a thief that observes
        the decremented bottom backs off on its own. Only the final task is
        genuinely contested, and that case alone reaches the lock.
        """
        bottom = self._bottom - 1
        self._bottom = bottom
        top = self._top

        if top > bottom:
            # Deque was already empty; undo the speculative decrement so bottom
            # is never left below top for the next observer.
            self._bottom = top
            return None

        task = self._array.get(bottom)

        if top < bottom:
            # At least one task still sits between the two ends, so no thief can
            # be contending for this one.
            return task

        # top == bottom: this is the last task, and a thief may be taking it.
        won = False
        with self._cas_lock:
            if self._top == top:
                self._top = top + 1
                won = True

        self._bottom = top + 1
        return task if won else None

    def steal(self) -> StealResult[T]:
        """
        Attempt to take the oldest task from the top. Safe for any thief thread.

        Reads the task before the CAS, exactly as the algorithm specifies. The
        read is speculative: if the swap fails the value is discarded and the
        task remains with whoever won, so a lost race costs a wasted read and
        nothing else.
        """
        top = self._top
        bottom = self._bottom

        if top >= bottom:
            return _EMPTY_RESULT

        # Snapshot the array reference; a concurrent grow may replace it, but
        # both copies agree on every index in the live window.
        task = self._array.get(top)

        with self._cas_lock:
            if self._top != top:
                return _ABORTED_RESULT
            self._top = top + 1

        return StealResult(StealStatus.SUCCESS, task)

    def try_steal(self, attempts: int = DEFAULT_STEAL_ATTEMPTS) -> StealResult[T]:
        """
        Steal with bounded retries, giving up on the first EMPTY observation.

        An abort means some other party just won a task from this deque, which
        is evidence the victim is worth another look. An empty deque is not, so
        the retry loop stops there instead of spinning on a drained worker.
        """
        if isinstance(attempts, bool) or not isinstance(attempts, int):
            raise ConfigurationError(
                f"attempts must be an integer, got {type(attempts).__name__}",
                parameter="attempts",
                value=attempts,
            )
        if attempts < 1:
            raise ConfigurationError(
                f"attempts must be at least 1, got {attempts}",
                parameter="attempts",
                value=attempts,
            )

        result: StealResult[T] = _EMPTY_RESULT
        for _ in range(attempts):
            result = self.steal()
            if not result.should_retry:
                return result
        return result

    def drain(self) -> List[T]:
        """
        Pop every remaining task, newest first.

        Intended for shutdown and for tests; a running worker should pop one
        task at a time so thieves retain something to take.
        """
        tasks: List[T] = []
        while True:
            task = self.pop()
            if task is None:
                return tasks
            tasks.append(task)

    def __repr__(self) -> str:
        return (
            f"ChaseLevDeque(size={len(self)}, capacity={self.capacity}, "
            f"top={self._top}, bottom={self._bottom})"
        )
