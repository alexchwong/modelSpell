"""Cancellation handle and per-call watch (spec v8 §6.10, §9.6).

A CancelHandle is shared by the caller and any number of complete() and complete_with_tools() calls. Each call creates a
private _Watch, which registers itself with the handle for the call's lifetime and holds a
duplicate of every socket the call opens. cancel() sets the handle's flag and, under the handle's
lock, shuts those duplicates down: a non-blocking operation that wakes whichever blocking socket
operation the calling thread is in. The lock is never held while caller code runs.
"""
from __future__ import annotations

from contextlib import contextmanager
import socket
import threading


class CancelHandle:
    """Cancels the complete() calls it is passed to. Thread-safe; cancel() never blocks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._watches: set[_Watch] = set()
        self._children: set[CancelHandle] = set()

    @property
    def cancelled(self) -> bool:
        """True once cancel() has been called. A handle never becomes uncancelled."""
        return self._cancelled

    def cancel(self) -> None:
        """Cancel every call using this handle now, and every later call. Returns at once."""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            for watch in self._watches:
                watch._abort_locked()  # shutdown() only: non-blocking
            for child in self._children:
                child.cancel()

    @contextmanager
    def _link(self, child: CancelHandle):
        with self._lock:
            if self._cancelled:
                child.cancel()
            self._children.add(child)
        try:
            yield
        finally:
            with self._lock:
                self._children.discard(child)

    def __repr__(self) -> str:
        return f"CancelHandle(cancelled={self._cancelled})"


class _Stop(Exception):
    """Cancellation seen at a checkpoint. Not an OSError, so urllib's do_open() lets it pass."""


class _Watch:
    """One complete() call's view of its handle (or of no handle: then it can never be cancelled)."""

    def __init__(self, handle: CancelHandle | None):
        self._h = handle
        self._lock = handle._lock if handle is not None else threading.Lock()
        self._dups: list[socket.socket] = []
        self.sent = False          # a byte of the completion request may have been written
        self._committed = False    # the call's ending is decided (spec v8 §6.10, commit point)
        self.in_connect = False    # inside connect(): proxy CONNECT bytes are not the request
        self._wake = threading.Event()  # set by cancel(): ends a retry backoff (0.4.0)

    def _cancelled(self) -> bool:
        return self._h is not None and self._h._cancelled

    def __enter__(self) -> _Watch:  # the pre-send check
        with self._lock:
            if self._cancelled():
                raise _Stop
            if self._h is not None:
                self._h._watches.add(self)
        return self

    def __exit__(self, *exc) -> None:
        with self._lock:
            if self._h is not None:
                self._h._watches.discard(self)
            dups, self._dups = self._dups, []
        for dup in dups:
            dup.close()

    def start_connect(self, sock: socket.socket, address) -> tuple[socket.socket, int]:
        """Register a duplicate and initiate a non-blocking connect, atomically (M30-15)."""
        with self._lock:
            if self._cancelled():
                raise _Stop
            dup = sock.dup()
            self._dups.append(dup)
            sock.setblocking(False)
            return dup, sock.connect_ex(address)  # returns at once (EINPROGRESS)

    def release(self, dup: socket.socket) -> None:
        """Forget the duplicate of a failed connection attempt."""
        with self._lock:
            if dup in self._dups:
                self._dups.remove(dup)
        dup.close()

    def begin_send(self) -> None:
        """Called before the first byte of the request is written."""
        with self._lock:
            if self._cancelled():
                raise _Stop
            self.sent = True

    def admit(self) -> None:
        """The admission point for one streamed fragment (spec v8 §6.10)."""
        with self._lock:
            if not self._committed and self._cancelled():
                raise _Stop

    def commit(self) -> None:
        """Terminal event, complete body, or clean end of a stream."""
        with self._lock:
            if self._committed:
                return
            if self._cancelled():
                raise _Stop
            self._committed = True

    def cancelled_before_commit(self) -> bool:
        """Before a package error propagates: did cancellation take effect first? Decides the ending."""
        with self._lock:
            if self._committed:
                return False
            self._committed = True
            return self._cancelled()

    def pause(self, delay: float) -> None:
        """Wait `delay` seconds between attempts (0.4.0 retry backoff); _Stop at once on cancellation."""
        with self._lock:
            if self._cancelled():
                raise _Stop
        if self._wake.wait(delay) or self._cancelled():
            raise _Stop

    def end_attempt(self) -> None:
        """Close the duplicates of a finished attempt's sockets before the next attempt connects."""
        with self._lock:
            dups, self._dups = self._dups, []
        for dup in dups:
            dup.close()

    def _abort_locked(self) -> None:
        if self._committed:
            return
        self._wake.set()
        for dup in self._dups:
            try:
                dup.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
