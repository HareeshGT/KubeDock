# KubeDock Web

KubeDock Web is the mobile-friendly companion dashboard built into the
KubeDock desktop application.

## How it works

When KubeDock starts, it starts the web server automatically in a background
thread. The web server does **not** create another SSH connection and does
not need SSH credentials of its own. It reuses the live SSH connection already
owned by KubeDock and opens managed SSH session channels for Kubernetes
commands and pod logs.

Web login credentials are configured in:

**KubeDock → Settings → Web App**

The credentials are read dynamically, so changing them does not require a
KubeDock restart.

## Open the web app

Keep KubeDock running, connect to the VM as usual, configure HTTPS, then open:

```text
http://<KubeDock-machine-LAN-IP>:8000
```

The web app is bound to `0.0.0.0:8000` by default so a phone on the same
network can reach it.

## Configuration

The `.env` file no longer contains SSH host/user/key/password settings or web
login credentials. The example file only documents optional web-server host
and port overrides.

For packaged KubeDock builds, the web UI also has a Python-embedded fallback
so the page remains available when the PyInstaller bundle does not include the
source `static/` directory.

## Security

The dashboard can delete pods and restart/scale deployments. Keep it on a
trusted LAN/VPN or put HTTPS/TLS in front of it before exposing it outside a
trusted network.


## HTTPS requirement

Remote non-loopback clients are rejected unless requests use HTTPS. Configure
`WEBAPP_TLS_CERTFILE` and `WEBAPP_TLS_KEYFILE` in the KubeDock process environment
to enable built-in Uvicorn TLS. The URL shown in Settings switches to https when both
are configured. Never expose the login endpoint over plain LAN HTTP.

The OTP seed is stored in the OS credential vault under
`KubeDock.WebApp.TOTP`. The last accepted TOTP counter (not the secret) is
persisted in settings to prevent replay after restart. Sessions expire after eight hours.
