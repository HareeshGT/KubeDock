# KubeDock

<p align="center">
  <strong>A desktop control center for AWS EC2, Kubernetes, SSH, SFTP, FTP, remote files, and cloud operations.</strong>
</p>

<p align="center">
  Manage infrastructure from one desktop application instead of switching between terminals, SSH clients, file managers, Kubernetes tools, and dashboards.
</p>

<p align="center">
  <a href="https://github.com/HareeshGT/KubeDock">
    <img src="https://img.shields.io/github/stars/HareeshGT/KubeDock?style=for-the-badge" alt="GitHub Stars">
  </a>
  <a href="https://github.com/HareeshGT/KubeDock">
    <img src="https://img.shields.io/github/forks/HareeshGT/KubeDock?style=for-the-badge" alt="GitHub Forks">
  </a>
  <a href="https://github.com/HareeshGT/KubeDock">
    <img src="https://img.shields.io/github/last-commit/HareeshGT/KubeDock?style=for-the-badge" alt="Last Commit">
  </a>
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-MIT-purple?style=for-the-badge" alt="MIT License">
  </a>
</p>

---

## Overview

**KubeDock** is a Python desktop infrastructure-management application built around common DevOps and cloud workflows.

It combines:

- AWS EC2 access
- SSH and SFTP
- Remote file management
- FTP
- Integrated terminal access
- Kubernetes management
- Pod logs and exec
- Port forwarding and tunnels
- AI-assisted operations
- An embedded mobile-friendly Web App
- Application security and settings

The goal is to make remote infrastructure feel like a single, consistent workspace.

---

# ✨ Features

## ☁️ AWS EC2 Management

Connect to and work with remote EC2 instances from the desktop application.

Features include:

- EC2 instance connections
- SSH connectivity
- Recent instance tracking
- Remote filesystem browsing
- Remote command execution
- File upload and download
- Remote file editing
- Search
- Media/file handling
- Sudo filesystem access

---

## 🖥️ Integrated SSH Terminal

KubeDock includes an integrated terminal for remote systems.

You can run normal Linux and infrastructure commands directly inside the application, for example:

```bash
kubectl get pods
systemctl status nginx
journalctl -u my-service
df -h
top
```

---

## 📁 Remote File Manager

A Finder-style interface for remote filesystems.

You can:

- Browse directories
- Create files and directories
- Rename files
- Delete files
- Upload files
- Download files
- Search files
- Edit files
- Execute files
- Stream remote video/audio directly over SSH/SFTP or FTP/FTPS (no download, no conversion)
- Work with privileged directories

---

## 🔐 Sudo Filesystem Access

KubeDock supports privileged filesystem operations when protected paths require elevated access.

This is intended to reduce repetitive manual privilege-escalation workflows when working with server files.

---

# ☸️ Kubernetes Management

KubeDock provides a dedicated Kubernetes management interface for common cluster operations.

Supported resource views include:

- Pods
- Deployments
- StatefulSets
- DaemonSets
- Services
- Ingress
- Jobs
- CronJobs
- Horizontal Pod Autoscalers
- Persistent Volumes
- Persistent Volume Claims
- ConfigMaps
- Secrets
- Events
- Storage resources

---

## 📊 Kubernetes Dashboard

The Kubernetes dashboard provides a high-level view of cluster activity, workloads, resources, events, and health information.

It is designed to reduce the need to start every investigation from commands such as:

```bash
kubectl get ...
```

---

## 📦 Pod Management

KubeDock provides pod-focused operational workflows including:

- Pod status
- Container information
- Restart counts
- Node information
- Pod details
- Pod logs
- Kubernetes events
- Container execution

Running containers can also be opened for interactive shell access where supported.

---

## 📜 Kubernetes Logs

Inspect pod and container logs directly from KubeDock.

Useful for investigating:

- Application failures
- CrashLoopBackOff
- Startup errors
- API failures
- Configuration problems
- Container errors

---

## 🖥️ Kubernetes Exec

Open an interactive shell inside a running container from the UI.

Typical workflows such as:

```bash
kubectl exec -it <pod> -- /bin/bash
```

or:

```bash
kubectl exec -it <pod> -- /bin/sh
```

can be performed without leaving KubeDock.

Output is **live**: the dialog keeps one persistent shell open in the pod, so a
long-running `wget`, `curl`, `apt` or `pip` shows its progress as it happens
(progress bars redraw in place, colours are preserved) instead of appearing only
after the command finishes. `cd`, environment variables and pipes work because
it is a real shell. Use the **Ctrl+C** button to interrupt a running command.
Closing the dialog ends the session in the pod.

> Full-screen programs that address the cursor (`vim`, `nano`, `top`, `htop`,
> `less`) are not supported in this dialog. Use `top -b -n1`, `cat`, or edit
> files with KubeDock's file editor instead.

---

## 🔀 Port Forwarding & Tunnels

KubeDock supports Kubernetes port forwarding and remote tunnel workflows.

This can be useful for accessing internal services such as:

- Grafana
- Prometheus
- Kibana
- Internal APIs
- Databases
- Development services
- Kubernetes dashboards

without directly exposing those services publicly.

---

# 🌐 Embedded Web App

KubeDock now includes an **embedded Web App** that starts automatically with the desktop application.

The Web App provides a mobile-friendly interface for monitoring and performing common Kubernetes operations from a phone, tablet, or another device that can reach the KubeDock host.

### Web App capabilities

- Mobile-friendly Kubernetes dashboard
- Namespace listing
- Pod listing
- Deployment listing
- Service listing
- Node listing
- Kubernetes events
- Pod/container logs
- Deployment restart
- Deployment scaling
- Pod deletion
- Basic authentication
- Health endpoint

### Existing SSH connection is reused

The embedded Web App does **not** create a second SSH connection to the VM.

KubeDock passes its existing live SSH client to the Web App runtime, and Web App Kubernetes operations are executed through that managed session.

This keeps the desktop and Web App on the same connection path and avoids unnecessary duplicate SSH sessions.

### Web App credentials

Web App credentials are configured from:

```text
Settings → Web App
```

You can configure:

- Web App username
- Web App password

The settings are loaded dynamically by the embedded server, so credential changes take effect without duplicating them in a separate Web App `.env` file.

### Web App logging

The embedded server writes logs to:

```text
~/.vm_visualizer/logs/webapp.log
```

### Web App configuration

The Web App can use environment variables for its listener configuration:

```text
WEBAPP_HOST
WEBAPP_PORT
```

The server normally binds to an externally reachable host address while keeping the UI login protected by Basic Authentication.

### Remote access

Because the Web App is a network service, remote-device access depends on network connectivity between the client and the machine running KubeDock.

A private network such as a VPN or mesh VPN can be used when you need to access the Web App while the phone and computer are on different physical networks.

---

# 👆 Swipeable Settings

The Settings dialog supports swipe-style navigation between its tabs.

You can:

- Swipe left to move to the next Settings tab
- Swipe right to move to the previous Settings tab
- Use horizontal trackpad gestures on supported systems
- Drag horizontally with the mouse
- Continue using normal tab clicks

The swipe can begin from the tab content area, so navigation does not depend on clicking the tab header itself.

---

# 🤖 AI / Ops Assistant

KubeDock includes AI-assisted infrastructure workflows.

Depending on the configured provider, AI functionality can help with:

- Troubleshooting
- Log analysis
- Explaining infrastructure errors
- Kubernetes investigation
- Command assistance
- Operational questions
- Pod diagnosis

AI provider settings are managed from the application Settings dialog.

---

# 🎨 UI, Themes & SVG Icons

KubeDock uses a consistent desktop UI with persistent theme settings and SVG-based icons.

The SVG icon system centralizes icon rendering through `ui_icons.py` and supports crisp HiDPI rendering, including Retina displays.

The application can map semantic labels to icons so controls can stay consistent across different parts of the UI.

---

# 🔒 Security

## Application Lock

KubeDock supports a PIN-protected application lock.

The app can lock automatically after a configurable inactivity period.

## PIN Storage

PIN-related security uses password-derived cryptographic storage rather than storing the PIN itself as plain text.

PBKDF2-SHA256 is used for PIN-related security.

## Web App Authentication

The embedded Web App requires a username and password configured in KubeDock Settings.

## Credential Handling

Do not commit private keys, passwords, API keys, tokens, or other secrets to the repository.

---

# 📡 FTP

KubeDock also provides FTP functionality for connected devices and remote filesystems.

The FTP interface is designed for graphical file-management workflows similar to the remote SFTP experience.

---

# 🧩 Architecture

KubeDock is being actively modularized so major infrastructure capabilities can evolve independently instead of putting application logic into one monolithic file.

Key areas include:

```text
KubeDock/
│
├── main.py
├── main_window.py
│
├── dashboard_tab.py
├── kubernetes_tab.py
├── k8s_cards.py
│
├── terminal_widget.py
├── editor_widgets.py
├── file_widgets.py
│
├── dialogs.py
├── workers.py
│
├── settings_dialog.py
├── themes.py
├── ui_icons.py
│
├── security.py
├── lock_screen.py
├── sudo_fs.py
│
├── ftp_fs.py
├── ai_assist.py
├── k8s_ai_ops.py
│
├── webapp/
│   ├── app.py
│   ├── server.py
│   ├── static/
│   │   └── index.html
│   └── ...
│
└── ...
```

---

# 🛠️ Technology Stack

KubeDock is primarily built with:

- Python
- PyQt / Qt
- Paramiko
- Kubernetes tooling
- `kubectl`
- AWS infrastructure
- FTP/SFTP tooling
- FastAPI
- Uvicorn
- AI APIs
- Shell commands
- SQLite-based local application persistence

Architecture at a high level:

```text
                    ┌──────────────────────┐
                    │       KubeDock       │
                    │   Desktop UI / App   │
                    └──────────┬───────────┘
                               │
             ┌─────────────────┼─────────────────┐
             │                 │                 │
             ▼                 ▼                 ▼
        AWS / EC2         Kubernetes       Remote Files
             │                 │                 │
             ▼                 ▼                 ▼
         SSH / SFTP        kubectl/API       SFTP / FTP
                               │
                               ▼
                       Embedded Web App
                               │
                               ▼
                       Phone / Tablet UI
```

---

# 💻 Requirements

KubeDock is primarily developed and tested on macOS.

Depending on the features you use, you may need:

- Python 3
- SSH client
- `kubectl`
- Kubernetes credentials / kubeconfig
- AWS credentials where required
- Appropriate SSH keys
- Network access to target systems

For Kubernetes workflows, verify that Kubernetes access is working in your environment.

```bash
kubectl config get-contexts
kubectl cluster-info
kubectl get nodes
```

---

# 🚀 Installation

## Clone the repository

```bash
git clone https://github.com/HareeshGT/KubeDock.git
cd KubeDock
```

## Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows:

```powershell
.venv\Scripts\activate
```

## Install dependencies

```bash
pip install -r requirements.txt
```

---

# ▶️ Running KubeDock

From the project directory:

```bash
python3 main.py
```

or:

```bash
python main.py
```

---

# ☸️ Kubernetes Setup

Check available contexts:

```bash
kubectl config get-contexts
```

Select the context you need:

```bash
kubectl config use-context <context-name>
```

Verify access:

```bash
kubectl get nodes
```

---

# ☁️ AWS Setup

For EC2 workflows, configure AWS credentials using the standard AWS mechanisms available in your environment.

For example:

```bash
aws configure
```

Verify:

```bash
aws sts get-caller-identity
```

---

# 🔑 SSH Setup

Make sure the SSH key has appropriate permissions:

```bash
chmod 600 ~/.ssh/my-key.pem
```

Test SSH access independently:

```bash
ssh -i ~/.ssh/my-key.pem ubuntu@<EC2-IP>
```

Once the SSH connection works, configure the corresponding connection details in KubeDock.

---

# ⚙️ Application Settings

The Settings dialog provides configuration for application behavior and infrastructure features.

Available areas include:

- Security / PIN lock
- Kubernetes tab visibility
- Protected Kubernetes namespaces
- AI providers, models, and API keys
- Web App username and password
- Theme and other application preferences

---

# 🌐 Web App Quick Start

1. Start KubeDock.
2. Connect to the VM as usual.
3. Open **Settings → Web App**.
4. Configure the Web App username and password.
5. Make sure the Web App port is reachable from the client device.
6. Open the Web App URL from your phone or tablet.

For devices on different physical networks, use a private VPN or mesh VPN between the devices or their networks.

---

# 🧪 Development

Clone the repository:

```bash
git clone https://github.com/HareeshGT/KubeDock.git
cd KubeDock
```

Create the environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the application:

```bash
python3 main.py
```

---

# 📦 Building the Application

KubeDock includes an installer script for packaging the application.

```bash
./installer.sh
```

The installer can work with a selected Git branch and packages the Web App static assets with the desktop application.

The current Web App UI source of truth is:

```text
webapp/static/index.html
```

During packaging, the installer also regenerates the packaged fallback Web UI from the current `index.html`, preventing an outdated embedded UI from being shipped.

---

# 🗂️ Typical Workflows

## Manage an EC2 server

```text
Launch KubeDock
      ↓
Connect to EC2
      ↓
SSH / SFTP
      ↓
Browse filesystem
      ↓
Open terminal
      ↓
Inspect logs
      ↓
Edit / transfer files
```

## Troubleshoot Kubernetes

```text
Open Kubernetes
      ↓
Select cluster / context
      ↓
Inspect namespace
      ↓
Check pods
      ↓
Open pod logs
      ↓
Inspect events
      ↓
Exec into container
      ↓
Investigate / fix
```

## Monitor Kubernetes from a phone

```text
Phone
  ↓
Private VPN / reachable network
  ↓
KubeDock Web App
  ↓
Existing KubeDock SSH session
  ↓
Remote kubectl
  ↓
Kubernetes cluster
```

---

# 🐛 Troubleshooting

## Kubernetes is not showing

Verify:

```bash
kubectl config get-contexts
kubectl cluster-info
kubectl get nodes
```

If these commands fail outside KubeDock, fix the Kubernetes configuration first.

## SSH connection fails

Test independently:

```bash
ssh -i <key> <user>@<host>
```

Check:

- Host/IP
- Username
- SSH key
- Key permissions
- Security groups
- Network connectivity
- SSH server availability

## Web App cannot be reached

Check that KubeDock is running and the embedded server has started.

Check the log file:

```bash
cat ~/.vm_visualizer/logs/webapp.log
```

Make sure the listening port is reachable from the client device and that the configured Web App credentials are correct.

If using a VPN or mesh VPN, verify both devices are connected to the same private network and can reach one another.

## Web App shows an old UI

The packaged UI should come from:

```text
webapp/static/index.html
```

Rebuild the application with:

```bash
./installer.sh
```

The installer regenerates the packaged fallback from the current Web App HTML during the build process.

## Permission denied on SSH key

Use:

```bash
chmod 600 <private-key>
```

## Port forwarding does not work

Check:

```bash
kubectl get pods
kubectl get svc
```

Then verify that the target service and port exist and are reachable.

---

# 🔐 Security Recommendations

Never commit secrets to this repository.

Do not commit:

```text
*.pem
*.key
.env
.env.*
credentials
tokens
passwords
API keys
private keys
```

Use environment variables, OS credential stores, SSH agents, or other secure credential-management mechanisms where appropriate.

Before publishing changes, review:

```bash
git status
git diff
```

---

# 🗺️ Roadmap

Potential future improvements include:

- More Kubernetes resource actions
- Improved multi-cluster workflows
- Better log analysis
- More AI-assisted troubleshooting
- More cloud-provider integrations
- Improved connection management
- More automation workflows
- Additional platform support
- More extensive automated testing
- Further UI modularization

The roadmap may change as the project evolves.

---

# 🤝 Contributing

Contributions are welcome.

Create a feature branch:

```bash
git checkout -b feature/my-feature
```

Make and test your changes.

Review them:

```bash
git diff
```

Commit:

```bash
git add .
git commit -m "Add my feature"
```

Push:

```bash
git push origin feature/my-feature
```

Then open a Pull Request.

---

# 🐞 Bug Reports

When reporting a bug, include:

- Operating system
- Python version
- KubeDock version or commit
- Relevant logs
- Steps to reproduce
- Expected behavior
- Actual behavior
- Kubernetes version, if applicable

Never include credentials, tokens, private keys, passwords, or other sensitive information in bug reports.

---

# 📸 Screenshots

Suggested documentation sections as the project evolves:

- Main dashboard
- EC2 manager
- Remote file manager
- SSH terminal
- Kubernetes dashboard
- Kubernetes resource views
- FTP manager
- AI / Ops assistant
- Web App
- Settings and themes

---

# 📜 License

KubeDock is licensed under the MIT License.

Copyright (c) 2026 Hareesh GT

See [`LICENSE`](LICENSE) for the complete license text.

---

# 👤 Author

**Hareesh GT**

GitHub:

https://github.com/HareeshGT

Project:

https://github.com/HareeshGT/KubeDock

---

# ⭐ Support the Project

If KubeDock is useful to you:

- ⭐ Star the repository
- 🐛 Report bugs
- 💡 Suggest improvements
- 🔧 Contribute code
- 📖 Improve documentation
- 🔀 Submit pull requests

---

<p align="center">

**KubeDock**

A unified desktop workspace for cloud, Kubernetes, and infrastructure operations.

Built for developers, DevOps engineers, SREs, and infrastructure teams.

</p>


## Local SQLite persistence

KubeDock stores local recent-connection metadata, Dashboard history, and
application settings in `~/.vm_visualizer/kubedock.db` using Python's built-in
`sqlite3` module. Existing `recent.csv`, dashboard history JSON, and
`settings.json` are read only during one-time migration; the legacy files are
retained and are never written by normal application operations. JSON used for
API payloads, editor language handling, and remote tunnel definitions is not
application-state persistence and is intentionally unchanged.
