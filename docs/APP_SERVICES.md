# Application Services Architecture

KubeDock remains a single desktop process. Feature services are in-process
Python components; only the optional Embedded Web App owns an HTTP server.

## Runtime and lifecycle

`AppServices` is the composition root. It creates one shared
`ConnectionService` and one `CommandService`, then injects them into the
feature services. It also registers services with `ServiceLifecycleManager`.
Registration is lazy and does not start polling or optional servers. Feature
services are marked RUNNING when their first real operation is requested;
dependencies are activated first.

The lifecycle manager provides explicit `STOPPED`, `STARTING`, `RUNNING`,
`STOPPING`, and `FAILED` states; dependency-aware start; stop/cleanup;
pause/resume/restart hooks; and credential-free diagnostic snapshots.
Services that do not implement a lifecycle hook remain passive objects. The
manager does not create threads or start work simply because a service exists.

`AppServices.service_status()` exposes service states, paused status, active-operation and
worker counts, last success/refresh, duration, and last error. Do not place
credentials, private-key paths, tokens, or raw connection objects in these
snapshots.

## Shared infrastructure

`ConnectionService` owns the active SSH or FTP/FTPS session, publishes
immutable connection snapshots, guards asynchronous work by session
generation, and tears down the managed transport. `CommandService` provides
the shared bounded command-worker factory and backend command executor. It
supports de-duplicating named in-flight operations; those keys are scoped to
the active connection generation so an old worker cannot be reused after a
reconnect. Diagnostics distinguish registered workers from currently active
operations, attribute instrumented workers to their owning feature, and update
last-success, last-error, duration, and refresh timestamps from worker results. Feature services must use these shared facilities rather than
opening a second primary connection.

## Feature services

- `KubernetesService`: Kubernetes commands, context-aware operations,
  workload/resource access, streams and tunnel configuration.
- `DockerService`: Docker CLI command policy, resource actions, and log streams. The Docker tab owns Qt presentation and the user-configured refresh timer; command construction and validation are delegated to the service.
- `DashboardService`: host/process/cluster telemetry command adapters.
- `FileManagerService`: filesystem operations and generation-bound transfers.
- `TerminalService`: terminal command workers.
- `AIService`: provider-backed explain, interpretation and investigation
  workers.
- `WebAppService`: access to the shared session for the optional FastAPI app.

The UI remains responsible for rendering. Remote commands, SSH/SFTP
operations, file transfers and AI calls must execute in workers, never on the
Qt UI thread. Dashboard and Docker refresh cadence must continue to honor
existing visibility and user preference policies.

## Shutdown

The main window invokes `AppServices.shutdown()`. It stops registered
service lifecycles, requests cancellation of outstanding command workers,
disconnects the shared transport to release blocked I/O, and performs a
bounded worker wait. Shutdown is idempotent and cleanup errors in one service
must not prevent transport teardown.


## Performance baseline

Run `python scripts/benchmark_service_runtime.py` from the repository root.
It records composition-root construction time, Python heap current/peak
allocations, optional process RSS (when `psutil` is installed), registered
service count, and worker counts. For a meaningful before/after comparison,
run the same script on the base revision and this branch with the same machine,
Python version, and installed dependencies. This benchmark intentionally
measures service-container startup only; it must not be presented as full GUI
startup or steady-state application memory.
