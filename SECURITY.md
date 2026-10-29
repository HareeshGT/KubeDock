# Security Policy

KubeDock is a desktop application for working with Kubernetes clusters, SSH connections, and configured AI providers. Security reports are welcome and help protect KubeDock users.

## Supported Versions

Security fixes are prioritized for the latest stable release. Pre-release builds and older releases may not receive fixes; users should upgrade to the latest stable release when possible.

| Version | Support status |
| --- | --- |
| Latest stable release | Supported |
| Older releases | Best effort; upgrade recommended |
| Development builds | Not guaranteed |

## Reporting a Vulnerability

**Please do not report security vulnerabilities in public GitHub issues, pull requests, or discussions.**

Use GitHub's private vulnerability reporting for this repository:

- [Report a vulnerability privately](https://github.com/HareeshGT/KubeDock/security/advisories/new)

If private vulnerability reporting is unavailable, contact the repository maintainer privately through GitHub before sharing technical details publicly. Do not publish exploit code or sensitive details until a fix or coordinated disclosure plan is in place.

When reporting, include as much of the following as you safely can:

- A clear description of the suspected vulnerability and its potential impact.
- The affected KubeDock version, operating system, and relevant configuration.
- Reproduction steps or a proof of concept that does not expose real credentials or systems.
- Any suggested mitigation, if known.

**Never include live API keys, passwords, access tokens, kubeconfig files, private keys, production logs containing secrets, or other sensitive data in a report.** Redact such values before sharing.

## Response and Disclosure

- We aim to acknowledge a report within **3 business days**.
- We aim to provide an initial assessment or status update within **7 calendar days** after acknowledgement.
- Investigation, remediation, and release timing depend on severity, reproducibility, and the complexity of the fix. We will communicate material status changes where possible.
- Please allow reasonable time to investigate and prepare a fix before public disclosure. We will coordinate disclosure with the reporter when feasible.
- If a report is not reproducible or is outside KubeDock's scope, we will explain that decision where possible.

These are response goals, not guaranteed deadlines.

## Security Considerations for Reports

KubeDock may be configured to connect to Kubernetes clusters, remote hosts, and third-party AI providers. When reporting a security issue, use a disposable test environment where possible and avoid testing against systems you do not own or have explicit permission to assess.
