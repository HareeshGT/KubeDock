# KubeDock

<p align="center">
  <strong>A desktop workspace for Kubernetes, virtual machines, SSH, remote files, Docker, and DevOps workflows.</strong>
</p>

<p align="center">
  <a href="https://github.com/HareeshGT/KubeDock"><img src="https://img.shields.io/github/stars/HareeshGT/KubeDock?style=for-the-badge" alt="GitHub Stars"></a>
  <a href="https://github.com/HareeshGT/KubeDock"><img src="https://img.shields.io/github/last-commit/HareeshGT/KubeDock?style=for-the-badge" alt="Last Commit"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-purple?style=for-the-badge" alt="MIT License"></a>
</p>

KubeDock brings everyday infrastructure tasks into one desktop app, so you can manage remote systems without constantly switching between terminals, file managers, and cluster tools.

## Features

- **Virtual machines & SSH:** Connect to EC2 and remote hosts, run commands, and manage remote files.
- **Remote file manager:** Browse, edit, upload, download, and search files over supported SSH/SFTP and FTP/FTPS connections.
- **Kubernetes:** Inspect clusters, workloads, services, storage, events, and logs; open container shells; and manage port forwards.
- **Docker:** Manage remote containers, images, volumes, and networks over SSH.
- **AI-assisted operations:** Analyze logs and workload signals, investigate common failures, and review recommendations. Remediation actions require the app's existing safeguards and confirmation.
- **Embedded Web App:** Use a mobile-friendly interface for common Kubernetes tasks.
- **Themes & settings:** Customize the desktop experience and configure supported AI providers.

## Installation

### Recommended: Fast prebuilt installer

Download the bundle for your operating system and architecture from the [Fast Installer release](https://github.com/HareeshGT/KubeDock/releases/latest).

The bundles target Linux x86-64, macOS Apple Silicon/Intel, and Windows x86-64. They omit optional Qwen3-TTS/PyTorch and Qt WebEngine/Chromium components to keep installation lighter.

On macOS or Linux, you can also run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/HareeshGT/KubeDock/main/installer/installer.sh -o kubedock-installer.sh
bash kubedock-installer.sh
```

Choose the installation mode with `KUBEDOCK_INSTALL_MODE`:

| Mode | Behavior |
| --- | --- |
| `auto` | Use a compatible prebuilt bundle when available; otherwise build from source. |
| `release` | Require a prebuilt bundle. |
| `source` | Build from source on the target machine. |

For a macOS package, run `bash installer/build_pkg.sh` on a Mac.

### Run from source

Requirements depend on the features you use. You will generally need Python 3, plus the relevant SSH, Kubernetes, or AWS tools and credentials for your environment.

```bash
git clone https://github.com/HareeshGT/KubeDock.git
cd KubeDock
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

On Windows, activate the environment with `.venv\\Scripts\\activate`. For local packaging, install `requirements-build.txt`. Optional voice, Monaco editor, and Qwen3-TTS dependencies are documented in their respective requirements files.

## AI Operations

KubeDock includes an AI Operations Centre alongside Ops Mind:

- Launch live, read-only incident collection directly from a pod's log viewer or the Service Index. It gathers bounded Kubernetes status, events, logs and metrics, and for cross-system investigations a minimal host summary and Docker container inventory. It does not query Kubernetes Secret values, ConfigMap values, container environment variables, or Docker logs.
- Runbook retrieval is local and opt-in. KubeDock indexes bounded Markdown/text files and includes matching excerpts in an investigation only when enabled for that investigation.
- Hosted incident investigations require explicit per-investigation consent. The Settings AI local-only option blocks hosted model calls and Google Web Speech transcription; Local / Private must be configured to use Ollama on loopback.
- Watch mode compares successive read-only observations, ignores ordinary Kubernetes AGE changes, can notify on meaningful state changes, and stops at its configured duration/observation limit.
- The right-sizing advisor accepts optional historical Prometheus P95 CPU/memory data over HTTPS (or localhost HTTP); at least five samples are required. It remains advisory and never changes workload resources.
- The remediation planner can prefill a supported proposal in Ops Mind for review, but it does not submit or execute the operation itself. Existing confirmation, protected-namespace, RBAC and verification controls remain in force.

Signal correlations and AI recommendations are hypotheses, not proof. Evidence is redacted and bounded before persistence/provider use, but redaction is a best-effort safeguard. Do not collect or share credential values.

## Kubernetes and cloud access

Configure your Kubernetes contexts, SSH connections, and AWS credentials using the standard tools for your environment. For example:

```bash
kubectl config get-contexts
kubectl get nodes
aws sts get-caller-identity
```

KubeDock can only access resources that the configured user, SSH identity, and Kubernetes/AWS permissions allow.

## Embedded Web App

The Web App provides a mobile-friendly view of common Kubernetes resources and operations. It reuses KubeDock's existing managed SSH session.

- Login uses a six-digit TOTP that rotates every 30 seconds, with expiring sessions.
- The listener defaults to loopback.
- **Remote access requires HTTPS.** Configure `WEBAPP_TLS_CERTFILE` and `WEBAPP_TLS_KEYFILE` before connecting from another device. Do not send OTPs over plain HTTP.
- Logs are written to `~/.kubedock/logs/webapp.log`.

## Security

KubeDock can connect to infrastructure and use credentials for remote systems and AI providers. Keep access credentials private, grant only the permissions you need, and never commit secrets to the repository.

Please report suspected vulnerabilities **privately**, not through public issues or pull requests. See the [Security Policy](SECURITY.md) and [private vulnerability reporting](https://github.com/HareeshGT/KubeDock/security/advisories/new).

## Development

Install the runtime dependencies as described above, then run:

```bash
python3 main.py
```

When contributing, create a feature branch, test your changes, and open a pull request. Do not include passwords, tokens, private keys, kubeconfig files, or sensitive logs in issues or pull requests.

## Troubleshooting

- **Kubernetes resources are missing:** Verify access with `kubectl config get-contexts` and `kubectl get nodes`.
- **SSH fails:** Check the host, username, key permissions, network access, and SSH server.
- **Web App is unreachable:** Check `~/.kubedock/logs/webapp.log`, the configured port, HTTPS settings, and the current OTP.
- **Fast installer unavailable:** Try `KUBEDOCK_INSTALL_MODE=source` to build from source if your machine has the required build dependencies.

## License

KubeDock is licensed under the [MIT License](LICENSE).

## Links

- [Repository](https://github.com/HareeshGT/KubeDock)
- [Fast Installer releases](https://github.com/HareeshGT/KubeDock/releases/latest)
- [Security Policy](SECURITY.md)
- [Report a vulnerability privately](https://github.com/HareeshGT/KubeDock/security/advisories/new)

---

<p align="center">
  <strong>KubeDock</strong><br>
  One desktop workspace for cloud, Kubernetes, and infrastructure operations.
</p>
