"""Shared remote command execution built on KubeDock's bounded SSH pool."""

from __future__ import annotations

from typing import Optional
import threading


class CommandService:
    """Create managed command workers for every feature using one SSH session.

    The service is intentionally the only place in the app service layer that
    constructs generic SSH command workers. Worker execution remains on Qt's
    QThread model and every command continues to use managed_exec_command.
    """

    def __init__(self, connection):
        self.connection = connection
        self._workers = []
        self._workers_lock = threading.RLock()
        self._shutting_down = False

    def register_worker(self, worker):
        """Keep service-created workers alive and include them in app shutdown."""
        with self._workers_lock:
            if worker not in self._workers:
                self._workers.append(worker)
                worker.finished.connect(
                    lambda w=worker: self._forget_worker(w)
                )
            shutting_down = self._shutting_down
        if shutting_down:
            self._request_worker_stop(worker)
        return worker

    def _forget_worker(self, worker):
        with self._workers_lock:
            if worker in self._workers:
                self._workers.remove(worker)

    @staticmethod
    def _request_worker_stop(worker):
        for method_name in ("cancel", "request_stop", "stop"):
            method = getattr(worker, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
                return
        try:
            worker.requestInterruption()
        except Exception:
            pass

    def request_worker_shutdown(self):
        with self._workers_lock:
            self._shutting_down = True
            workers = list(self._workers)
        for worker in workers:
            self._request_worker_stop(worker)

    def wait_for_workers(self, timeout_seconds=3.0):
        deadline = __import__("time").monotonic() + max(0.0, float(timeout_seconds))
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            try:
                if worker.isRunning():
                    remaining_ms = max(0, int((deadline - __import__("time").monotonic()) * 1000))
                    if remaining_ms <= 0:
                        break
                    worker.wait(remaining_ms)
            except Exception:
                pass
        with self._workers_lock:
            self._workers = [w for w in self._workers if getattr(w, "isRunning", lambda: False)()]

    def create_worker(
        self,
        worker_pool: Optional[list],
        command: str,
        *,
        cwd: Optional[str] = None,
        sudo_user: Optional[str] = None,
        timeout: Optional[int] = None,
        raw: bool = False,
    ):
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before running remote commands")

        # Lazy imports avoid coupling service startup to Qt worker modules.
        from workers import CommandWorker, track_worker

        worker = CommandWorker(
            ssh, command, cwd=cwd, sudo_user=sudo_user,
            timeout=timeout, raw=raw,
        )
        worker.connection_generation = snapshot.generation
        worker.connection_session_id = snapshot.session_id
        worker._session_guard = lambda generation=snapshot.generation: self.connection.is_current(generation)
        self.register_worker(worker)
        if worker_pool is not None:
            track_worker(worker_pool, worker)
        return worker

    def execute(
        self, command: str, *, timeout: int = 30, input_data: bytes = None,
        expected_generation: int = None, expected_ssh=None,
    ):
        """Execute a bounded command synchronously for backend/service callers.

        UI features should use create_worker() so they never block the Qt
        event loop. Output streams are drained concurrently to avoid a full
        stderr pipe blocking stdout (or the reverse), and a timer closes a
        stalled channel at the requested deadline.
        """
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before running remote commands")
        if expected_generation is not None and snapshot.generation != expected_generation:
            raise RuntimeError("The active connection changed before the command could start.")
        if expected_ssh is not None and ssh is not expected_ssh:
            raise RuntimeError("The active connection changed before the command could start.")
        from workers import managed_exec_command

        deadline = max(0.1, float(timeout))
        with managed_exec_command(
            ssh, command, channel_timeout=deadline,
        ) as (stdin, stdout, stderr):
            channel = stdout.channel
            timed_out = threading.Event()
            drain_errors = []
            out_chunks = []
            err_chunks = []

            def abort_channel():
                timed_out.set()
                try:
                    channel.close()
                except Exception:
                    pass

            def drain(stream, chunks):
                try:
                    chunks.append(stream.read())
                except Exception as exc:
                    drain_errors.append(exc)

            timer = threading.Timer(deadline, abort_channel)
            timer.daemon = True
            timer.start()
            out_thread = threading.Thread(
                target=drain, args=(stdout, out_chunks), daemon=True,
            )
            err_thread = threading.Thread(
                target=drain, args=(stderr, err_chunks), daemon=True,
            )
            try:
                channel.settimeout(deadline)
                if input_data is not None:
                    stdin.write(input_data)
                    stdin.flush()
                    channel.shutdown_write()
                out_thread.start()
                err_thread.start()
                code = channel.recv_exit_status()
                out_thread.join(timeout=deadline)
                err_thread.join(timeout=deadline)
                if timed_out.is_set():
                    raise TimeoutError(
                        "Remote command timed out after {} seconds".format(timeout)
                    )
                if out_thread.is_alive() or err_thread.is_alive():
                    raise TimeoutError(
                        "Remote command output did not close after {} seconds".format(timeout)
                    )
                if drain_errors:
                    raise drain_errors[0]
                out = b"".join(out_chunks).decode("utf-8", errors="replace")
                err = b"".join(err_chunks).decode("utf-8", errors="replace")
            except Exception as exc:
                if timed_out.is_set():
                    raise TimeoutError(
                        "Remote command timed out after {} seconds".format(timeout)
                    ) from exc
                raise
            finally:
                timer.cancel()

        if not self.connection.is_current(snapshot.generation):
            raise RuntimeError(
                "The active connection changed while the remote command was running; "
                "the stale result was discarded."
            )
        return code, out, err
