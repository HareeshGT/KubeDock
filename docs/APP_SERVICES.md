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
- Receives degraded, recovered, and lost health transitions from the single
  existing `ConnectionHealthWorker`; it does not start another heartbeat.
- Closes the existing secondary SSH pool before the filesystem adapter and
  primary SSH client.
- Makes disconnect and application shutdown idempotent.

`CommandService` is the shared command-worker factory and backend executor.
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
| `KubernetesService` | Kubernetes command execution over the selected connection | Kubernetes resources, tunnels, Ops Mind |
| `DockerService` | Docker command execution over the selected connection | Docker tab |
| `DashboardService` | Host, process and cluster telemetry commands | Dashboard refresh pipeline |
| `FileManagerService` | Filesystem access, directory listing and streaming file readers | Remote file manager |
| `TerminalService` | Integrated terminal and working-directory command workers | Main terminal |
| `AIService` | Provider-backed explain, Kubernetes interpretation and investigation worker construction | Pod AI, terminal explain, Ops Mind |
| `WebAppService` | Restricted access to the active session and shared backend command execution | Embedded FastAPI application |

Feature widgets receive the shared services from the main window. Their
legacy `ssh` accessors now resolve through `ConnectionService`; the stored
legacy value is only a compatibility fallback for standalone widget tests.
Feature workers are created through their domain service in the desktop app.
The embedded Web App is passed `WebAppService` and resolves its active SSH
client and command executor from the same `AppServices` instance.

## Threading and security boundaries

- Network handshakes, command execution, directory listing, and streaming
  reads stay off the Qt UI thread.
- The Web App retains its existing authenticated desktop bridge for UI
  mutations; HTTP request threads do not call Qt widgets directly.
- SSH host-key verification, sudo/command safeguards, Kubernetes mutation
  confirmation and verification, RBAC checks, operation audit records, and
  AI evidence redaction remain in their existing paths.
- Service snapshots do not contain passwords, private-key contents, or
  private-key paths.
- The one existing SSH health monitor is retained to avoid duplicate probes.

## Compatibility and next cleanup

This migration introduces real service ownership and service-first operation
factories while retaining compatibility fallbacks for widgets created outside
the main application. Those fallbacks should only be removed after standalone
widget tests and all remaining legacy call sites have been migrated. New
features should depend on `AppServices` or a narrow service interface and
must never create their own SSH connection or bypass the managed command
service.
