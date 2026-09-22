# CortexAI User Flow Diagram Source

This file is a diagram-ready source of truth for user-facing runtime flows across all layers.

## Render Options

1. Mermaid Live Editor
- Open https://mermaid.live
- Paste any `mermaid` block from this document.
- Export as SVG or PNG.

2. Mermaid CLI
- Save a diagram block into a `.mmd` file.
- Run:
```bash
npx -y @mermaid-js/mermaid-cli -i <diagram>.mmd -o <diagram>.svg
```

## Layered End-to-End Flow (User Perspective)

```mermaid
flowchart TB
    %% L0
    subgraph L0["Layer 0 - User"]
        U["User in Browser"]
    end

    %% L1
    subgraph L1["Layer 1 - Frontend Experience"]
        FE1["frontend-react/dist/index.html"]
        FE2["frontend-react/src\nUI state + mode selection + streaming parser"]
        FE3["User actions\nsingle chat | compare | optimize | history | reports | byok"]
    end

    %% L2
    subgraph L2["Layer 2 - API Edge and Request Plumbing"]
        A1["FastAPI app factory\nserver/app.py"]
        A2["RequestIDMiddleware\nserver/middleware.py"]
        A3["Auth dependency\nget_api_key() in server/dependencies.py"]
        A4["Request schemas + guardrails\nserver/schemas/requests.py + server/utils.py"]
    end

    %% L3
    subgraph L3["Layer 3 - Route Handlers"]
        R1["/v1/chat + /v1/chat/stream\nserver/routes/chat.py"]
        R2["/v1/compare + /v1/compare/stream\nserver/routes/compare.py"]
        R3["/v1/optimize\nserver/routes/optimize.py"]
        R4["/v1/history\nserver/routes/history.py"]
        R5["/v1/usage + /v1/savings\nserver/routes/reporting.py"]
        R6["/v1/byok\nserver/routes/byok.py"]
        R7["/v1/providers + opt-in /v1/models + /v1/whoami\ncatalog.py + whoami.py"]
    end

    %% L4
    subgraph L4["Layer 4 - Orchestration and Runtime Policy"]
        O1["CortexOrchestrator.ask() / compare()\norchestrator/core.py"]
        O2["Prompt/Context processing\nprompt optimizer + research enrichment"]
        O3["Smart routing stack\nPromptAnalyzer -> TierDecider -> ModelSelector -> FallbackManager -> ResponseValidator"]
        O4["Circuit breaker + fallback policy\nserver/circuit_breaker.py"]
        O5["Unified response contract\nmodels/unified_response.py"]
    end

    %% L5
    subgraph L5["Layer 5 - Provider Adapter Layer"]
        P1["ClientRegistry + ProviderAdapter\napi/client_registry.py + api/provider_adapter.py"]
        P2["Provider clients\nopenai/gemini/deepseek/grok clients"]
    end

    %% L6
    subgraph L6["Layer 6 - External AI APIs"]
        X1["OpenAI API"]
        X2["Gemini API"]
        X3["DeepSeek API"]
        X4["Grok API"]
    end

    %% L7
    subgraph L7["Layer 7 - Persistence, Governance, Reporting"]
        D1["Preflight\nresolve_and_enforce_usage_caps()\nrate limit + daily caps + API key ownership"]
        D2["BYOK key resolution\nresolve_runtime_byok_provider_keys()"]
        D3["Persistence writes\npersist_chat_interaction()/persist_compare_interaction()"]
        D4["Repository + SQL tables\nsessions/messages/llm_requests/llm_responses/routing_decisions/routing_attempts/usage_daily/llm_savings/byok_provider_keys"]
    end

    %% L8
    subgraph L8["Layer 8 - Response Delivery"]
        S1["DTO/NDJSON serialization\nserver/schemas/responses.py"]
        S2["Frontend render\nstream cards + summaries + history sidebar"]
    end

    U --> FE3
    FE3 --> FE2
    FE2 --> FE1
    FE2 --> A1
    A1 --> A2 --> A3 --> A4
    A4 --> R1
    A4 --> R2
    A4 --> R3
    A4 --> R4
    A4 --> R5
    A4 --> R6
    A4 --> R7

    R1 --> D1
    R2 --> D1
    R6 --> D1
    R4 --> D1
    R5 --> D1
    R7 --> D1

    R1 --> D2
    R2 --> D2

    R1 --> O1
    R2 --> O1
    R3 --> O2
    O1 --> O2 --> O3 --> O4 --> O5
    O5 --> P1 --> P2
    P2 --> X1
    P2 --> X2
    P2 --> X3
    P2 --> X4

    O5 --> R1
    O5 --> R2
    R1 --> D3
    R2 --> D3
    R4 --> D4
    R5 --> D4
    R6 --> D4
    R7 --> D4
    D3 --> D4

    R1 --> S1
    R2 --> S1
    R3 --> S1
    R4 --> S1
    R5 --> S1
    R6 --> S1
    R7 --> S1
    S1 --> S2 --> U
```

## User Journey: Single Chat (Streaming)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant FE as frontend app
    participant API as /v1/chat/stream
    participant MW as middleware+auth
    participant PF as persistence preflight
    participant GB as generation budget resolver
    participant BYOK as runtime BYOK resolver
    participant ORCH as CortexOrchestrator.ask
    participant SR as Smart routing stack
    participant REG as ClientRegistry
    participant PC as Provider client
    participant LLM as External provider API
    participant PERSIST as persist_chat_interaction

    User->>FE: Submit prompt
    FE->>API: POST /v1/chat/stream (prompt+routing+context+generation)
    API->>MW: request_id + X-API-Key validation
    API->>GB: resolve profile, reasoning, and effective ceiling
    GB-->>API: one provider+billing budget
    opt DB mode enabled
        API->>PF: resolve owner + authorize exact effective ceiling + rate limit
        API->>BYOK: resolve tenant provider keys
    end
    API->>ORCH: ask(...)
    ORCH->>SR: plan route and fallback policy
    loop attempt until valid response or stop
        ORCH->>REG: create_client(provider, model)
        REG-->>ORCH: client
        ORCH->>PC: get_completion(messages, resolved generation params)
        PC->>LLM: provider API request
        LLM-->>PC: raw completion
        PC-->>ORCH: UnifiedResponse
        ORCH->>ORCH: validate + circuit breaker + fallback
    end
    ORCH-->>API: final UnifiedResponse
    API-->>FE: NDJSON start/line/response_done(status+budget+retry)/done
    opt DB mode enabled
        API->>PERSIST: save session+messages+request+response+routing+usage+savings
    end
    FE-->>User: Render streamed answer and metadata
```

## User Journey: Compare (Streaming, Multi-Target)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant FE as frontend app
    participant API as /v1/compare/stream
    participant MW as middleware+auth
    participant PF as persistence preflight
    participant GB as generation budget resolver
    participant BYOK as runtime BYOK resolver
    participant ORCH as CortexOrchestrator.compare
    participant MMO as MultiModelOrchestrator
    participant PC as Provider clients (N)
    participant LLM as External APIs
    participant PERSIST as persist_compare_interaction

    User->>FE: Submit compare prompt with targets
    FE->>API: POST /v1/compare/stream (shared/target generation)
    API->>MW: request_id + X-API-Key validation
    API->>GB: resolve each target's effective ceiling/reasoning
    GB-->>API: per-target provider+billing budgets
    opt DB mode enabled
        API->>PF: authorize exact per-target ceilings + rate limit
        API->>BYOK: resolve tenant provider keys
    end
    API->>ORCH: compare(...)
    ORCH->>MMO: get_comparisons_sync(...)
    par each target
        MMO->>PC: get_completion(..., target generation params)
        PC->>LLM: provider API request
        LLM-->>PC: provider response
        PC-->>MMO: UnifiedResponse
    end
    MMO-->>ORCH: MultiUnifiedResponse
    ORCH-->>API: normalized compare responses
    API-->>FE: NDJSON response_start/line/response_done per target + done(compare summary)
    opt DB mode enabled
        API->>PERSIST: persist grouped requests with request_group_id
    end
    FE-->>User: Render side-by-side model outputs and summary
```

## User Journey: History, Reporting, BYOK

```mermaid
flowchart LR
    U["User"] --> FE["frontend app"]

    FE --> H["GET/DELETE /v1/history"]
    FE --> R["GET /v1/usage, /v1/savings, exports"]
    FE --> B["POST/GET/DELETE /v1/byok"]

    H --> A["Auth + request_id"]
    R --> A
    B --> A

    A --> K["Resolve API key owner (DB mode)"]
    K --> DBH["History repository reads/writes"]
    K --> DBR["Usage/Savings aggregates + CSV"]
    K --> DBB["BYOK encrypted key + api_key settings"]

    DBH --> FE
    DBR --> FE
    DBB --> FE
    FE --> U
```

## Layer-to-Code Map

- Layer 1 (Frontend): React `frontend-react/src/` + built `frontend-react/dist/index.html`
- Layer 2 (Edge/Middleware/Auth): `server/app.py`, `server/middleware.py`, `server/dependencies.py`
- Layer 3 (Routes): `server/routes/*.py`
- Layer 4 (Orchestration): `orchestrator/core.py`, `orchestrator/smart_router.py`, `orchestrator/*`
- Layer 5 (Adapters): `api/client_registry.py`, `api/provider_adapter.py`, `api/*_client.py`
- Layer 6 (External APIs): OpenAI, Gemini, DeepSeek, Grok
- Layer 7 (Persistence/Governance): `server/persistence.py`, `db/repository.py`, `server/rate_limit.py`, `server/privacy.py`, `server/savings.py`
- Layer 8 (Response Contracts): `server/schemas/responses.py`, frontend stream/event rendering in `frontend-react/src/api/` + `frontend-react/src/hooks/`

