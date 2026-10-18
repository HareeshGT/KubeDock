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
- Kubernetes: inspect namespaces, pods, deployments, services, nodes, events, and pod logs; restart/scale deployments and delete pods; select a session-scoped Kubernetes context.
- Machines: interactive PTY terminal over authenticated WebSocket, SFTP directory browsing, text editing, upload/download (5 MB file-transfer cap), create directory, and delete files or empty directories.
- Docker: list containers, images, volumes, networks, and Compose applications on the active SSH machine; view container logs; start, stop, restart, and remove containers.
- System: read-only process, disk, listening-port, service, and basic metrics inspection where supported by Linux, macOS, or Windows PowerShell.
- Desktop: inspect connection state, request a dashboard/tunnel refresh, disconnect the active machine with exact-target confirmation, update the desktop theme and tunnel CSV path, and start/stop configured SSH tunnels.
- Operations: view metadata-only operation audit records. Command output and file contents are not stored in the audit database.

## Safety constraints and current limitations

- This is a single-active-SSH-target interface. It does not yet provide a saved machine inventory or switch among multiple concurrent SSH sessions.
- Kubernetes resource editing/apply for arbitrary YAML, full CRUD for every resource type, pod exec, and a full cluster registration/removal workflow are not exposed yet.
- Docker mutations are currently limited to containers; image, volume, network, and Compose operations are read-only.
- System inspection is read-only. Process termination, service/package/firewall management, reboot, and shutdown are not exposed.
- File editing is UTF-8 text only and capped at 1 MB. Binary transfer is capped at 5 MB. Recursive directory deletion is intentionally not available.
- System operations depend on the connected account's OS permissions. The web app does not elevate privileges or bypass remote authorization.
- Browser WebSocket terminal access grants the authenticated user the connected SSH account's shell permissions. Protect the web endpoint accordingly.

## Validation

Run locally:

    python -m compileall -q webapp tests
    QT_QPA_PLATFORM=offscreen python -m pytest -q

The GitHub Actions workflow .github/workflows/remote-control-tests.yml runs these checks on the feature branch. Passing automated tests does not replace a live smoke test against a disposable SSH host and Kubernetes cluster.
