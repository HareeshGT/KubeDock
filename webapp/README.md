# KubeDock Web

KubeDock Web is the mobile-friendly companion dashboard built into the
KubeDock desktop application.

## How it works

When KubeDock starts, it starts the web server automatically in a background
thread. The web server does **not** create another SSH connection and does
not need SSH credentials of its own. It reuses the live SSH connection already
owned by KubeDock and opens managed SSH session channels for Kubernetes
commands and pod logs.

The current six-digit TOTP is displayed in **KubeDock → Settings → Web App**. The seed is generated once and stored in the host OS credential vault. The FastAPI backend independently validates each code; no username/password fallback remains.

## Open the web app

Keep KubeDock running, connect to the VM as usual, configure HTTPS, then open:

```text
https://<KubeDock-machine-LAN-IP>:8000
```

For remote access, configure both `WEBAPP_TLS_CERTFILE` and `WEBAPP_TLS_KEYFILE` in the KubeDock process environment. Without TLS, remote requests are rejected; plain HTTP is allowed only through loopback (for example, `http://127.0.0.1:8000`).

By default the web app listens on `127.0.0.1:8000` (loopback only). When both
`WEBAPP_TLS_CERTFILE` and `WEBAPP_TLS_KEYFILE` are set it listens on
`0.0.0.0:8000` instead, so a phone on the same network can reach it over HTTPS.
Set `WEBAPP_HOST` to override either default.

## Configuration

The `.env` file no longer contains SSH host/user/key/password settings or web
login credentials. The example file only documents optional web-server host
and port overrides.

For packaged KubeDock builds, the web UI also has a Python-embedded fallback
so the page remains available when the PyInstaller bundle does not include the
source `static/` directory.

## Security

The dashboard can delete pods and restart/scale deployments. Remote access must use HTTPS or an SSH tunnel that terminates at loopback. Do not expose the login endpoint over plain LAN HTTP.


## HTTPS requirement

Remote non-loopback clients are rejected unless requests use HTTPS. Configure
`WEBAPP_TLS_CERTFILE` and `WEBAPP_TLS_KEYFILE` in the KubeDock process environment
to enable built-in Uvicorn TLS. The URL shown in Settings switches to https when both are configured. Never expose the login endpoint over plain LAN HTTP.

The OTP seed is stored in the OS credential vault under
`KubeDock.WebApp.TOTP`. The last accepted TOTP counter (not the secret) is
persisted in settings to prevent replay after restart. Sessions expire after eight hours.
