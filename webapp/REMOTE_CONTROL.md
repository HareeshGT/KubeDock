# KubeDock remote-control interface

This interface is embedded in the running KubeDock desktop process. It is not a standalone multi-host control plane.

## Secure setup

1. Keep KubeDock running and connect to the target machine through the desktop app.
2. Configure WEBAPP_TLS_CERTFILE and WEBAPP_TLS_KEYFILE in the KubeDock process environment before starting the web server.
3. Open the HTTPS URL shown by KubeDock Settings → Web App, then sign in using the current six-digit OTP shown in that same settings page.
4. Do not expose port 8000 directly to the public internet. Prefer a private network or VPN and firewall rules restricted to trusted clients. An SSH tunnel to loopback is also supported.
5. The web session expires after eight hours. Logging out invalidates the browser session.

Without TLS, the server should bind to loopback and rejects non-loopback requests. Never put SSH passwords, private keys, OTP seeds, or cloud credentials into browser code or URL parameters.

## Target and scope

- Kubernetes requests use the active SSH connection's remote kubectl installation and kubeconfig.
- Selecting a Kubernetes context applies --context per authenticated web session. It does not change the shared kubeconfig current-context.
- Machine file, Docker, and system-inspection APIs target the active SSH connection owned by KubeDock. The browser cannot choose arbitrary hosts or submit SSH credentials.
- Desktop status, refresh, disconnect, theme settings, tunnel settings, and local SSH port-forward lifecycle use a queued bridge back to the KubeDock UI thread.
- SSH tunnels bind local ports on the KubeDock desktop machine, not on the browser device. Tunnel start requires the existing desktop SSH connection to use a private-key file.

## Available capabilities

- Overview: inspect the active SSH target, host facts, and desktop connection status.
- Kubernetes: inspect common resources, namespaces, pods, deployments, services, nodes, events, and pod logs; restart/scale deployments, delete pods or allowlisted resources, browse/edit resource YAML, validate it with a server-side dry run, and apply YAML after explicit confirmation; select a session-scoped Kubernetes context. Secret values are redacted from resource browsing and Secret YAML is not fetched.
- Machines: interactive PTY terminal over authenticated WebSocket, SFTP directory browsing, text editing, upload/download (5 MB file-transfer cap), create directory, and delete files or empty directories.
- Docker: list containers, images, volumes, networks, and Compose applications on the active SSH machine; view container logs; start, stop, restart, and remove containers; remove unused images, volumes, and networks with exact-target confirmation.
- System: process, disk, listening-port, service, and basic metrics inspection where supported by Linux, macOS, or Windows PowerShell; terminate a specific process, start/stop/restart a named service on Linux/Windows, and schedule reboot/shutdown only after exact hostname and phrase confirmation.
- Desktop: inspect connection state, request a dashboard/tunnel refresh, disconnect the active machine with exact-target confirmation, update the desktop theme and tunnel CSV path, and start/stop configured SSH tunnels.
- Operations: view metadata-only operation audit records. Command output and file contents are not stored in the audit database.

## Safety constraints and current limitations

- This is a single-active-SSH-target interface with a saved machine inventory. It can switch sequentially among saved key-based SSH profiles, but it does not hold multiple concurrent SSH sessions and cannot reconnect to password-only profiles from the web UI.
- Full CRUD for every Kubernetes resource type, pod exec, and a full cluster registration/removal workflow are not exposed yet. YAML apply supports arbitrary manifests up to 1 MB, while the resource browser is limited to an allowlist.
- Docker Compose application lifecycle controls are read-only. Image, volume, and network removal is available but Docker itself may reject removal while a resource is in use.
- Package installation/removal, firewall changes, and arbitrary system commands are not exposed. Process termination, service actions, reboot, and shutdown require explicit confirmation and remain subject to remote account privileges.
- File editing is UTF-8 text only and capped at 1 MB. Binary transfer is capped at 5 MB. Recursive directory deletion is intentionally not available.
- System operations depend on the connected account's OS permissions. The web app does not elevate privileges or bypass remote authorization.
- Browser WebSocket terminal access grants the authenticated user the connected SSH account's shell permissions. Protect the web endpoint accordingly.

## Validation

Run locally:

    python -m compileall -q webapp tests
    QT_QPA_PLATFORM=offscreen python -m pytest -q

The GitHub Actions workflow .github/workflows/remote-control-tests.yml runs these checks on the feature branch. Passing automated tests does not replace a live smoke test against a disposable SSH host and Kubernetes cluster.
