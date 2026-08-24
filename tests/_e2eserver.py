"""#846 - one way to run the app in-process for an e2e test, and one way to stop it.

THE FLAKE THIS EXISTS TO END. Every e2e file started uvicorn with
`threading.Thread(target=lambda: uvicorn.run(app, ...), daemon=True)` and never stopped it.
`uvicorn.run()` has no shutdown handle, so when `main()` returned the interpreter began
finalizing with a live server thread still in it, and the run intermittently died with
`Python runtime state: finalizing (tstate=...)`. Seen on 2026-08-19 during #842: the suite
reported 293/294 with e2e_ask.py failing that way, while the same file passed standalone 3
times in a row. A green suite that can randomly report a failure teaches the reader to
distrust the number, which is the same harm as a guard that cannot go red.

TWO THINGS ARE FIXED HERE, NOT ONE.

  * SHUTDOWN. `uvicorn.Server` exposes `should_exit`, so the server is asked to stop and the
    thread is joined before the process ends. Teardown is deterministic instead of racing
    interpreter finalization.

  * READINESS. The old pattern slept a flat 1.5s and hoped. `server.started` is the real
    signal, so a slow bind no longer produces a connection-refused failure that looks like a
    product bug, and a fast one stops costing 1.5s per file. The wait also notices a server
    thread that DIED, which previously showed up as an inexplicable refused connection.
"""
import threading
import time

import uvicorn


class ServerDidNotStart(RuntimeError):
    pass


class _Serving:
    def __init__(self, server, thread):
        self.server = server
        self.thread = thread

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=timeout)


def serving(app, host: str, port: int, *, log_level: str = "warning",
            timeout: float = 20.0) -> _Serving:
    """Start `app` on host:port in a background thread and return once it is ACTUALLY
    listening. Use as a context manager, or keep the handle and call `.stop()`.

    The thread stays daemon so a hard crash in the test body cannot wedge the process - but
    the normal path stops the server explicitly, which is the whole point.
    """
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=log_level))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + timeout
    while not server.started:
        if not thread.is_alive():
            raise ServerDidNotStart(
                f"the server thread died before {host}:{port} was listening - the port is "
                f"probably already in use by another test (see #702)")
        if time.monotonic() > deadline:
            server.should_exit = True
            thread.join(timeout=5)
            raise ServerDidNotStart(f"{host}:{port} did not start within {timeout:.0f}s")
        time.sleep(0.02)
    return _Serving(server, thread)
