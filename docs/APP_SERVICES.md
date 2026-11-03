# Application Services Architecture

KubeDock remains a single desktop process. Feature services are in-process
Python components; only the optional Embedded Web App owns an HTTP server.

## Runtime and lifecycle

`AppServices` is the composition root. It creates one shared
`ConnectionService` and one `CommandService`, then injects them into the
feature services. It also registers services with `ServiceLifecycleManager`.
Registration is lazy and does not start polling or optional servers.

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
the shared bounded command-worker factory and backend command executor.
Feature services must use these shared facilities rather than opening a
second primary connection.

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
