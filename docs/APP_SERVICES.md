# Application Services Architecture

KubeDock is a single desktop application process. The services under
`services/` are in-process application components; they are not separate
OS processes, HTTP servers, or microservices.

## Runtime composition

```text
QApplication
  └── KubeDockWindow
       └── AppServices
            └── ConnectionService
                 ├── active SSH client (when SSH is selected)
                 ├── app-facing SFTP/SudoFS adapter
                 └── FTP/FTPS filesystem session (instead of SSH)
```

Connection workers remain responsible for performing network handshakes off
the UI thread. When a worker succeeds, the main window adopts the resulting
session into `ConnectionService`. The service then owns its lifecycle until
it is replaced or disconnected.

## ConnectionService responsibilities

- Keep the active SSH or FTP/FTPS session and credential-free connection
  metadata in one place.
- Publish immutable `ConnectionSnapshot` values to subscribers. Snapshots
  contain state, protocol, target, generation, session ID, connection time,
  optional latency, and a bounded last-error string. They never contain
  passwords, private-key material, or private-key paths.
- Expose a generation check so asynchronous work can reject results produced
  for an older connection after a reconnect.
- Translate the existing heartbeat worker's at-risk/recovered/lost signals
  into shared connection state.
- Close the existing secondary SSH connection pool before closing the
  app-facing filesystem and primary SSH client.
- Make disconnect and application shutdown idempotent.

The service does not start its own heartbeat. KubeDock's existing
`ConnectionHealthWorker` remains the single health monitor, avoiding a
second competing monitor.

## Existing worker infrastructure is retained

The existing `SSHConnectionPool`, managed-channel limits, and
`managed_exec_command` implementation remain the command-channel mechanism.
The new service owns the session and pool lifecycle; it does not replace the
bounded pool with one shared SSH channel. Multiple operations still need
independent channels, but those channels are obtained through the managed
pool.

`CommandWorker` and feature tabs still have compatibility paths that accept
the raw Paramiko client. This is intentional for the first migration slice.
Use `ConnectionService.get_ssh_client()` only at these legacy boundaries.
New domain services should depend on `AppServices` or a narrow operation
interface rather than opening their own SSH connections or retaining raw
clients.

## Current integration

- `LifecycleMixin` creates one `AppServices` instance for the window.
- SSH and FTP/FTPS connection success callbacks adopt sessions into the
  shared `ConnectionService`.
- Disconnect and window shutdown delegate resource teardown to that service.
- The existing connection-health callbacks update shared degraded, recovered,
  and lost states.
- The embedded Web App obtains its SSH client from the same service. It does
  not create an independent SSH session.

## Migration sequence

1. Add a command execution service that uses the managed SSH channel pool and
   preserves worker timeouts and cancellation behavior.
2. Move Kubernetes, Docker, and Dashboard remote operations behind domain
   services sharing the same connection service.
3. Move remote file operations and terminal session creation behind explicit
   file/terminal interfaces.
4. Route AI/Ops Mind evidence collection through read-only domain service
   methods rather than opening connections independently.
5. Keep the Web App on a restricted adapter over the same runtime and retain
   the existing Qt desktop bridge for UI-affecting operations.

Each migration should preserve current RBAC, command allowlists, operation
confirmation, host-key verification, channel limits, UI-thread boundaries,
and worker lifecycle guarantees.
