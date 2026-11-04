# KubeDock × Design Intelligence System (DIS)

## What is integrated

KubeDock exposes `AppServices.design_intelligence`, an optional HTTP client for the **real** Design Intelligence System API. The client calls the DIS service endpoints; it does not generate local placeholder agent results.

- `GET /health` — health check
- `GET /api/v1/agents` — actual registered DIS agent definitions
- `POST /api/v1/design/runs` — submit a KubeDock design brief to DIS

The client is disabled by default and does not make network calls until configured.

## Run DIS locally or configure a deployment

For local development, start the DIS repository in its own terminal after installing its requirements:

```bash
cd DesignIntelligenceSystem
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In KubeDock, open **AI Intelligence → Design Intelligence**, enter `http://127.0.0.1:8000`, provide a brief, and select **Run DIS design review**. The API call runs in a background worker so the UI remains responsive. The URL is stored in KubeDock settings.

Alternatively, set `KUBEDOCK_DIS_URL` in the environment where KubeDock starts, pointing at a reachable DIS API deployment, for example:

```bash
export KUBEDOCK_DIS_URL="https://your-dis-service.example.com"
```

Do not include `/api/v1` in the base URL. Use HTTPS for remote deployments. Keep private deployments behind authentication/network controls; this initial client does not send credentials or implement authenticated transport.

Example from KubeDock Python code:

```python
from services import AppServices, DesignIntelligenceError

services = AppServices()
try:
    services.design_intelligence.health()
    agents = services.design_intelligence.list_agents()
    run = services.design_intelligence.run_design_review(
        "Review the Kubernetes dashboard for hierarchy, usability and accessibility.",
        constraints=["Preserve existing theme support", "Accessible keyboard interactions"],
        target_platforms=["macOS", "Windows"],
        project_id="kubedock",
    )
finally:
    services.shutdown()
```

Handle `DesignIntelligenceError` and present it to the user; do not substitute fabricated results when DIS is unavailable. The request is a design-planning call and does not grant DIS permission to edit KubeDock files or deploy changes.

## Current capability boundary — important

The current DIS repository describes its orchestrator as **deterministic first-pass orchestration**. The HTTP integration calls the actual service and returns its actual response, but the DIS specialists are not yet connected to LLM providers and do not edit code, launch a browser, capture screenshots, or verify rendered UI. Do not describe this stage as autonomous AI design execution or visual QA.

Next DIS milestones for genuine agent execution:
1. Provider-backed specialist reasoning with explicit model configuration, structured outputs, retries and evaluation.
2. Authenticated, scoped repository execution adapter that proposes patches rather than silently applying risky changes.
3. Browser/screenshot capture and visual inspection, with evidence attached to each QA result.
4. Iterative refinement and regression testing.
5. Project-scoped memory and quality history.

## Safety and data handling

- Send only the brief, product context and constraints needed for the design task.
- Never include API keys, credentials, cluster secrets, production logs or unrelated user data in design briefs.
- The current client has bounded timeouts and response size, validates response shape, and reports transport errors instead of faking success.
- DIS output is advisory. KubeDock's existing confirmation and execution safeguards remain authoritative for all infrastructure mutations.
- A configured endpoint is a network trust boundary. Only use a DIS deployment you control or trust.
