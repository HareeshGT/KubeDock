# Application Services Architecture

KubeDock remains a single desktop application process. The `services/`
package contains in-process application services, not a process or HTTP server
per feature and not separately deployed microservices.

## Runtime composition

```text
QApplication
  └── KubeDockWindow
       └── AppServices
            ├── ConnectionService
            │    ├── active SSH client + SFTP/SudoFS adapter
            │    └── active FTP/FTPS filesystem session (instead of SSH)
            ├── CommandService
            │    └── existing bounded SSHConnectionPool / managed channels
            ├── KubernetesService
            ├── DockerService
            ├── DashboardService
            ├── FileManagerService
            ├── TerminalService
            ├── AIService
            └── WebAppService
```

`AppServices` is created once by the main window. Every feature service
receives the same `ConnectionService` and, where relevant, the same
`CommandService`. No feature service establishes a separate SSH connection.
The shared command service also keeps a process-local registry of service-owned
workers. On application shutdown it requests cancellation/stopping across
those workers, closes the shared connection to release blocked network I/O,
and waits for worker threads for a bounded interval before teardown completes.

## Shared connection and command lifecycle

Connection workers continue to perform SSH and FTP/FTPS handshakes away from
the Qt UI thread. A successful session is adopted by `ConnectionService`,
which owns the active transport and app-facing filesystem adapter until
replacement or disconnect.

`ConnectionService`:
- Publishes immutable, credential-free `ConnectionSnapshot` state to
  subscribers. Snapshots contain protocol, target, session ID, generation,
  connected time, optional latency, and bounded error information.
- Exposes generation checks so async consumers can identify stale work after
  a reconnect.
- Creates and generation-binds the single existing `ConnectionHealthWorker`; it receives degraded, recovered, and lost transitions and is stopped when its owning session is replaced or disconnected. It does not start another heartbeat.
- Closes the existing secondary SSH pool before the filesystem adapter and
  primary SSH client.
- Makes disconnect and application shutdown idempotent.

`CommandService` is the shared command-worker factory and backend executor. The reusable PTY-backed `ExecStreamWorker` lives in `workers_parts/`, not in the file-execution dialog module, so Kubernetes, Docker, and file services depend on the worker layer rather than a UI dialog implementation.
UI operations use its QThread worker factory, preserving existing timeouts,
signals, tracking, and cancellation paths. Backend Web App commands use its
synchronous executor, which concurrently drains stdout and stderr and closes
the channel at the configured deadline.

The existing `SSHConnectionPool`, managed-channel limits, and
`managed_exec_command` remain in use. Connection sharing does not mean every
operation must share one SSH channel: each concurrent operation still uses a
bounded managed channel from the existing pool.

## Feature service responsibilities and integration

| Service | Responsibility | Current consumers |
|---|---|---|
| `ConnectionService` | Active SSH/FTP(S) session, health state, replacement and teardown | Main window, feature tabs, Web App |
| `CommandService` | Shared bounded remote command workers and backend command execution | Kubernetes, Docker, Dashboard, terminal, Ops Mind, Web App |
| `KubernetesService` | Kubernetes command workers, context application, generation-guarded pod shells, log streams, and evidence collection | Kubernetes resources, tunnels, Ops Mind, pod Exec and Logs dialogs |
| `DockerService` | Docker command execution and generation-guarded live log streams | Docker tab and Docker log dialog |
| `DashboardService` | Host, process and cluster telemetry workers, including selected-context binding for cluster snapshots | Dashboard refresh pipeline |
| `FileManagerService` | Filesystem access, metadata lookup, byte writes, path normalization, create/delete/rename, directory listing, guarded streaming readers, remote file execution, and shared adapter access for media/transfer dialogs | Remote file manager, Edit, Run, Play, Upload, Download, and large-file viewer dialogs |
| `TerminalService` | Integrated terminal and working-directory command workers | Main terminal |
| `AIService` | Provider-backed explain, Kubernetes interpretation and investigation worker construction | Pod AI, terminal explain, Ops Mind |
| `WebAppService` | Restricted access to the active session and shared backend command execution | Embedded FastAPI application |

Feature widgets receive the shared services from the main window. Their
legacy `ssh` accessors now resolve through `ConnectionService`; the stored
legacy value is only a compatibility fallback for standalone widget tests.
Feature command workers are created through their domain service in the desktop app. Command, directory, file-stream, file-transfer, and media-start workers capture the session generation and reject stale work after a reconnect. `ConnectionService` actively cancels generation-bound workers when a session is replaced, lost, or disconnected, so blocking transfer handles are closed promptly rather than waiting for the next result check. FTP/FTPS downloads and uploads use dedicated per-transfer control/data connections, leaving the shared browsing control channel available for directory and metadata operations. FTP stream timeouts propagate as transfer errors rather than being mistaken for EOF, preventing a stalled read from silently producing a truncated file. The remote file manager obtains its filesystem adapter through `FileManagerService` for dialogs and transfer helpers, while metadata lookup, byte writes, common path and mutation operations are service methods. The editor's streaming read and save paths, regular uploads/downloads, and large-file viewer downloads all receive the shared file service so their workers are generation-bound and registered for shutdown. The optional local OpenSSH `scp` optimization is also bound to that generation and is terminated if the application session changes; it is a transfer subprocess, not a replacement for or owner of the app's primary SSH session.
The embedded Web App is passed `WebAppService` and routes command
execution, short-lived SFTP channel creation, and interactive PTY channel
creation through the shared service boundary. These channels use the active
SSH transport; the Web App never creates a separate SSH client. The app also
retains the Qt desktop bridge for UI-affecting actions, and command dispatch is
bound to the same SSH client that was validated for that request.

## Threading and security boundaries

- Network handshakes, command execution, directory listing, file transfers,
  media stream setup, and streaming reads stay off the Qt UI thread.
- The Web App retains its existing authenticated desktop bridge for UI
  mutations; HTTP request threads do not call Qt widgets directly.
- SSH host-key verification, sudo/command safeguards, Kubernetes mutation
  confirmation and verification, RBAC checks, operation audit records, and
  AI evidence redaction remain in their existing paths.
- Service snapshots do not contain passwords, private-key contents, or
  private-key paths.
- A feature service must not establish an independent primary SSH or
  file-transfer session. Connection workers establish the user session;
  `ConnectionService` adopts and owns it.
- The Dashboard's FTP/FTPS telemetry worker is a deliberate exception for
  read-only observation: it opens a short-lived probe control connection so
  dashboard `PWD`/`FEAT`/directory metadata commands cannot interleave with
  uploads or downloads on the user's active file-transfer session. It closes
  that probe in `finally`; it is not used for file mutations or as a second
  application session.
- Async worker results are discarded when their captured session generation
  is no longer current.
- The one existing SSH health monitor is retained to avoid duplicate probes.

## Compatibility and next cleanup

This migration introduces real service ownership and service-first operation
factories while retaining compatibility fallbacks for widgets created outside
the main application. Those fallbacks should only be removed after standalone
widget tests and all remaining legacy call sites have been migrated. New
features should depend on `AppServices` or a narrow service interface and
must never create their own SSH connection or bypass the managed command
service.
