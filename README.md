# GreenPrompt

A four-person hackathon project that routes prompts to appropriately sized AI models, checks supported answers, and reports estimated resource savings.

## Current status

This is a fresh project scaffold, not the earlier simulated prototype. Only the health endpoint and a minimal frontend connection check are implemented. Model calls, routing, validation, accounting, and storage remain assigned work. `/chat` explicitly returns HTTP 501 until integration is ready.

## Structure and ownership

```text
frontend/                  Frontend member
  index.html
  style.css
  script.js
backend/
  app.py                   Backend member: HTTP and orchestration
  schemas.py               Backend member: shared API models
  model_clients.py         Backend member: provider connections
  config.py                Backend member: environment configuration
  requirements.txt         Backend member: shared dependencies
  .env.example             Backend member: configuration template
  router.py                Jarvis: classification, selection, escalation
  validator.py             Jarvis: answer checks
  metrics.py               Metrics member: usage and savings
  storage.py               Metrics member: SQLite and summaries
tests/                     Each owner tests their component
docs/
  api-contract.md          Shared interface; coordinate changes
  team-workflow.md         Integration order and working agreements
```

## Local setup

Use Python 3.10 or newer. From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r backend/requirements.txt
Copy-Item backend/.env.example backend/.env
.venv\Scripts\python -m uvicorn backend.app:app --reload --port 8000
```

In another terminal, serve the frontend:

```powershell
python -m http.server 5500 --directory frontend
```

Open http://localhost:5500. The starter page can check backend health. Interactive backend documentation is at http://localhost:8000/docs.

The configuration template is documentation for the scaffold; environment-file loading is part of the backend member's implementation. Set environment variables directly until that is added. No provider keys are needed for the health endpoint.

## Tests

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

Test modules currently contain assignment notes, not completed tests. A zero-test run is not proof that the application works.

## First integration milestone

Agree on `docs/api-contract.md`, then get one real model answer through `/chat` and display it in the frontend. Develop the decision functions and metrics against sample data in parallel. Keep mocks explicit; never silently replace real model failures with canned answers.

Do not commit credentials or generated databases. All environmental and initial cost figures are illustrative estimates, not measured footprints or provider prices.
