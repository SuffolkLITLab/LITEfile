---
id: architecture
title: System architecture & technologies
sidebar_label: Architecture
sidebar_position: 2
---

# System architecture & technologies <span className="wip-badge">WIP</span>

LITEfile is designed around a clean separation of concerns between user session state, declarative jurisdiction configurations, cloud document storage, and third-party court APIs.

---

## High-level architecture diagram

```mermaid
graph TB
    subgraph Client Layer
        Browser[User Web Browser]
    end

    subgraph LITEfile Web App [Django ASGI App]
        NginxWhiteNoise[WhiteNoise Static File Server]
        Router[Django URL Router & Workflow Middleware]
        Views[View Controllers & API Endpoints]
        ConfigLoader[YAML Jurisdiction Config Loader]
        DraftService[Filing Drafts & Plan Service]
        LLMHelper[AI Document Extraction Engine]
    end

    subgraph Data & Storage
        DB[(PostgreSQL / SQLite)]
        S3[(AWS S3 Document Storage)]
    end

    subgraph External Services
        OpenAI[OpenAI / LLM Gateway]
        EFSP[Tyler EFSP / State Court E-Filing API]
    end

    Browser <-->|HTTPS| Router
    Router --> Views
    Views --> ConfigLoader
    Views --> DraftService
    Views --> LLMHelper
    DraftService --> DB
    Views <-->|Pre-signed URLs| S3
    LLMHelper <--> OpenAI
    Views <-->|REST API| EFSP
```

---

## Core components

### 1. Application layer (`efile_app/`)
- **Django 5.2 ASGI core**: Runs using Gunicorn with Uvicorn worker threads (`uvicorn.workers.UvicornWorker`) for asynchronous performance and scalability.
- **Workflow state engine (`efile/workflow.py`)**: Tracks draft state and selects the new- or existing-case path, conditional person details and case questions, and returns from Review edits. Server validation checks required answers and documents before submission.
- **Custom authentication backend (`efile/authentication.py`)**: Integrates directly with the Tyler E-Filing authentication endpoint, validating credentials against the state EFSP and creating local user profiles dynamically.

### 2. Document handling & S3 storage
- Uploaded court PDFs are validated (file format, size limits, corruption checks) and securely uploaded to AWS S3.
- All document access utilizes temporary, short-lived **Pre-Signed URLs**, ensuring buckets remain private while enabling Tyler's ingestion servers to fetch documents during envelope submission.

### 3. Declarative jurisdiction YAML system
- YAML configuration files (`static/config/states/*.yaml`) define case categories, filing codes, document checklist rules, court fees, and contact info.
- Loaded into an in-memory cache on application startup, allowing court rules to change without touching application code.
