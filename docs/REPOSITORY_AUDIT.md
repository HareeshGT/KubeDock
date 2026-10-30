# KubeDock Repository Audit and Reliability Improvements

**Audit date:** 2026-10-30  
**Base branch:** `main`  
**Implementation branch:** `audit/comprehensive-reliability-security`

## Scope inspected

The audit used the current repository tree and latest merged commit as the source of truth. The inspected areas included:

- Application persistence and SQLite fallback/migration in `db.py`.
- App-lock PIN hashing and persistence in `security.py`, with the PIN setup flow in `lock_screen.py` and `settings_dialog.py`.
- Web App authentication/session handling, remote operation endpoints, and audit persistence in `webapp/server.py`, `webapp/otp_auth.py`, and `webapp/operation_audit.py`.
- Regression tests for database paths, AI security, Kubernetes mutation verification, Web App authentication, remote control, and operation auditing.
- Installer versioning and release workflow, prebuilt validation workflow, runtime/build/optional dependency files, README, security policy, and AI enhancement status.

This report records confirmed findings and implemented changes; it is not a claim that every interactive feature has been manually exercised against every supported operating system or a live infrastructure environment.

## Confirmed findings and fixes

### High — Existing SQLite data could be silently bypassed when fallback copying failed

**Affected files:** `db.py`, `tests/test_db_paths.py`

When the preferred application-data directory was unavailable, the fallback path attempted to back up an existing legacy SQLite database. A failed backup was swallowed, allowing startup to continue with a new empty database. That could make existing user settings and profiles appear to have disappeared.

**Fix:** Fail closed with an actionable `sqlite3.OperationalError` when an existing database cannot be preserved. The original database is left untouched; KubeDock no longer silently proceeds as if recovery had succeeded.

**Regression coverage:** Simulate a database-copy failure and assert that the failure is surfaced and the original database remains readable.

### Medium — PIN validation existed in the dialog but was not enforced by the security API

**Affected files:** `security.py`, `tests/test_security.py`

The settings dialog enforced a 6-character minimum, but `security.set_pin()` itself accepted shorter or non-string values. A caller bypassing the dialog could persist a PIN that the unlock path would not accept.

**Fix:** Enforce the existing UI contract at the security boundary: the PIN must be a string of 6–64 characters before a salt/hash is generated or settings are persisted.

**Regression coverage:** Empty, short, non-string, and over-limit PINs are rejected without persisting settings; valid PIN setup stores the hash rather than plaintext.

### Medium — Web App failed-login tracking could grow without a bound

**Affected files:** `webapp/server.py`, `tests/test_webapp_auth.py`

The in-memory rate-limit map retained entries for distinct client addresses and had no global size limit. A listener exposed to many distinct source addresses could accumulate unnecessary state.

**Fix:** Prune expired entries and cap tracked client addresses at 2,048, evicting the oldest active entry when the cap is reached.

**Regression coverage:** Verify that the tracking map remains bounded under many distinct client addresses.

### Regression prevention — HTTP API authentication coverage

**Affected file:** `tests/test_webapp_auth.py`

Added a regression check that every HTTP `/api/*` route except the intended login/logout entry points declares the `require_login` dependency. The interactive WebSocket terminal is excluded from this HTTP dependency check because it validates the session, origin, and transport in its own handshake handler.

## CI and dependency audit

Added `.github/workflows/dependency-audit.yml`. It runs `pip-audit` and `pip check` against separate core, build, editor, and voice dependency profiles. The voice profile installs the native PortAudio headers required to build PyAudio on Ubuntu.

The optional `requirements-qwen-tts.txt` stack is not installed by this recurring CI job because it pulls a source-pinned, heavyweight ML/TTS dependency stack. Its upstream commit pin remains a manual review item; this workflow does not claim to audit that optional environment.

## Validation results

GitHub Actions on implementation commit `13180406ccef968ac0f606c9f3bb6a070c66815a`:

- Full regression suite: **275 passed, 2 skipped, 144 subtests passed**.
- Dependency audit matrix: core, build, editor, and voice profiles completed successfully.
- The regression workflow compiled application and test modules before running pytest.

Relevant runs:

- [Full regression suite](https://github.com/HareeshGT/KubeDock/actions/runs/41036408548)
- [Dependency vulnerability audit](https://github.com/HareeshGT/KubeDock/actions/runs/41036408566)

## Remaining validation and limitations

- The environment used for this audit could access GitHub's repository APIs but could not resolve `github.com` for a local Git checkout. Changes were therefore prepared through the repository's Git interface and verified with GitHub Actions rather than a local interactive desktop session.
- Cross-platform frozen installer builds and smoke tests are exercised by the prebuilt-validation workflow on a pull request; they have not been claimed as completed by this branch's regression run.
- Live Kubernetes, SSH, Docker, and remote file workflows were not tested against production infrastructure. Their existing mocked regression tests were retained.
- The Qwen3-TTS optional environment is not included in the automated dependency audit, as noted above.

## Follow-up

Review the final pull request's regression, dependency-audit, and prebuilt-validation checks before merging. Do not publish a release as part of this audit.
