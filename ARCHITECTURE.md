# Agentic Labor — EU-Sovereign Architecture

Frankfurt-hosted, GDPR-aligned, multi-agent back-office automation for Putsch Mittelstand.

---

## Unified Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│             USER / SACHBEARBEITER (Putsch Mittelstand)           │
└────────────────────────────────┬─────────────────────────────────┘
                                 │
              ┌──────────────────▼──────────────────┐
              │   LangGraph Durable Orchestrator    │
              │   (Postgres checkpointer, HITL)     │  ← top layer
              │   workflow = AP, Sales, Customs...  │
              └──────────────────┬──────────────────┘
                                 │ A2A protocol
   ┌─────────────────────────────┼─────────────────────────────┐
   │                             │                             │
┌──▼────────────┐         ┌──────▼─────────┐         ┌─────────▼────────┐
│ CrewAI:       │         │ CrewAI:        │         │ Magentic-One     │
│ AP / Kred.    │         │ Sales Order    │         │ Pattern:         │
│ Crew (4-6     │         │ Crew           │         │ Customs/Master   │
│ specialists)  │         │                │         │ Data Crew        │
└──┬────────────┘         └──────┬─────────┘         └─────────┬────────┘
   │                             │                             │
   └───── all agents call ───────┼───── via MCP ──────────────┘
                                 │
   ┌─────────────────────────────┼─────────────────────────────┐
   │           DSPy MODULES (compiled per-task)                │
   │   ExtractInvoice  MatchToPO  ClassifyHS  RouteApproval    │
   └─────────────────────────────┬─────────────────────────────┘
                                 │
   ┌─────────────────────────────┼─────────────────────────────┐
   │                    MCP SERVERS (tools)                    │
   │   SAP-MCP   DATEV-MCP   Docling-MCP   Atlas-MCP (customs) │
   │   Zep/Graphiti-MCP (memory)  Email-MCP  SharePoint-MCP    │
   └─────────────────────────────┬─────────────────────────────┘
                                 │
                  ┌──────────────▼──────────────┐
                  │   LLM ROUTER (LiteLLM)      │
                  │   per-task model selection  │
                  └──────────────┬──────────────┘
                                 │
   ┌─────────────────────────────┼─────────────────────────────┐
   │  vLLM / TGI cluster (Frankfurt) — open-weights models     │
   │  + Mistral La Plateforme API (Paris, EU jurisdiction)     │
   └───────────────────────────────────────────────────────────┘
```

Cross-cutting: Langfuse (OTel collector → Postgres + ClickHouse, Frankfurt VPC).
Memory: Zep + Graphiti on Neo4j (Frankfurt VPC).

---

## Deployment Topology — Frankfurt EU-Sovereign

**Primary region:** AWS Frankfurt (eu-central-1) or Hetzner Falkenstein/Nuremberg for full German data residency. Hetzner is the harder-core sovereignty choice — no US-headquartered hyperscaler in the data path.

**Compute:**
- **Application tier** (CrewAI + LangGraph + DSPy): K8s on managed nodes, 3 AZs.
- **LLM inference:** Dedicated GPU pool (H100 or L40S) running vLLM with Mistral Large 2, Qwen2.5-72B, Qwen2.5-Coder-32B, Llama 3.3 70B. Granite-Docling 258M on cheap L4s.
- **Frontier reasoning fallback:** Mistral La Plateforme API — Paris-based, GDPR-aligned, contractually EU-only data processing. Avoid Anthropic/OpenAI for production data flows; use them only for development and red-teaming.

**Storage:** Postgres (LangGraph checkpoints, Langfuse), ClickHouse (Langfuse traces), Neo4j (Zep/Graphiti), MinIO or S3-compatible object store for documents.

**Networking:** Private VPC, all SAP/DATEV connections via dedicated VPN or SAP Private Link. No egress to non-EU endpoints in production paths.

**Identity:** Keycloak for SSO into Langfuse, LangGraph Studio (if used), and internal admin UIs. OAuth 2.1 with PKCE for all MCP server connections (now standard per AAIF 2026 guidance — 81% of remote MCP servers already use it).

**Compliance posture:** GDPR by design (data minimization in Langfuse via masking), SOC 2 trajectory via Drata/Vanta, AVV agreements with Mistral.

---

## LLM-to-Task Map

The "swap models per task" matrix.

| Task | Primary model | Why | Fallback |
|---|---|---|---|
| German invoice field extraction | Mistral Large 2 (or Mistral Medium 3.5) | EU jurisdiction, strongest open-weights German, 128k context | Qwen2.5-72B |
| Document layout / OCR | Granite-Docling 258M (VLM) | Purpose-built, runs on L4, MIT, IBM-maintained | Qwen2.5-VL-72B |
| SAP / DATEV code generation | Qwen2.5-Coder-32B | 92.7% HumanEval, Apache 2.0, runs locally | DeepSeek-Coder-V2 |
| Long-context reasoning (audit replay, multi-doc) | Llama 4 Scout (long context) or DeepSeek V3 | 1M+ tokens, MIT | Mistral Large 2 |
| Manager / orchestrator reasoning | Mistral Large 2 or DeepSeek R1 | Strong planning / tool-use, EU-friendly | Llama 3.3 70B |
| Cheap classification (HS codes, routing, intent) | Qwen3-14B or Phi-4 | Fast, cheap, fine-tunable on Putsch data | Mistral 7B |
| German email / customer comms drafting | Mistral Large 2 | Best-in-class German prose | Qwen2.5-72B |
| Eval / LLM-as-judge | DeepSeek V3 | Cheap, strong reasoning, MIT | Llama 3.3 70B |

Route all of this through LiteLLM (or Mistral's OpenAI-compatible endpoints) so DSPy's `dspy.configure(lm=...)` swap is a one-line change per task.

---

## 30-Day Stand-Up Plan

### Week 1 — Foundations (Days 1–7)

- **Day 1–2:** Provision Frankfurt VPC, K8s cluster, Postgres + ClickHouse + Neo4j managed instances.
- **Day 3–4:** Stand up Langfuse (self-hosted, OSS image) and LiteLLM proxy. Wire CrewAI sample crew to Langfuse via OTel.
- **Day 5–7:** Deploy vLLM cluster with Mistral Large 2 (via Mistral La Plateforme initially — switch to self-hosted weights when GPU capacity arrives) and Qwen2.5-72B. Smoke-test inference latency and German output quality on 20 sample Eingangsrechnungen.

### Week 2 — Document Pipeline (Days 8–14)

- **Day 8–9:** Install Docling + Granite-Docling. Build a CLI that ingests 100 real Putsch invoices and exports DoclingDocument JSON.
- **Day 10–12:** Build the first DSPy module — `ExtractInvoiceFields` — with a Pydantic signature for German invoice fields (USt-IdNr, Leistungsdatum, Skonto, Netto/Brutto/MwSt). Hand-label 50 invoices for the eval set.
- **Day 13–14:** Run GEPA optimization. Measure accuracy on holdout. Target: ≥95% field-level accuracy with Qwen2.5-72B + BAML adapter. If not, fall back to Mistral Large 2.

### Week 3 — Orchestration (Days 15–21)

- **Day 15–17:** Build the first CrewAI Crew — AP Kreditorenbuchhaltung — with 4 agents (Extractor, PO-Matcher, DATEV-Coder, Exception-Router). Connect to Docling-MCP and SAP-MCP servers (build the SAP-MCP server yourself; it's ~200 lines wrapping PyRFC).
- **Day 18–19:** Wrap the Crew inside a LangGraph node with `PostgresSaver` checkpoints and an `interrupt()` for Sachbearbeiter approval on exceptions > €10k.
- **Day 20–21:** Add Zep / Graphiti as the memory layer. Episode = each completed invoice. Test temporal queries ("show me how this vendor's payment terms changed over the last 6 months").

### Week 4 — Hardening + Second Workflow (Days 22–30)

- **Day 22–24:** Implement Magentic-One pattern for a more complex workflow — Customs Declaration with WebSurfer (HS code lookup on EZT-Online), FileSurfer (commercial invoice + packing list), and an SAP-Booking specialist. Either use AutoGen's `MagenticOneGroupChat` or reimplement the orchestrator-ledger loop inside LangGraph.
- **Day 25–27:** Build the eval harness in Langfuse — 200 hand-labeled cases per workflow. LLM-as-judge with DeepSeek V3. Wire to CI so every prompt / model change runs the eval.
- **Day 28–30:** Red-team. Inject malformed invoices, hostile emails, ambiguous customs cases. Measure escalation rate, false-positive rate, $/invoice. Ship the first internal pilot to one Putsch business unit.

**Exit criterion at Day 30:** One end-to-end workflow (AP) processing real invoices with full Langfuse traces, Zep memory, DSPy-compiled extraction, and LangGraph durable execution. ≥90% straight-through processing on routine invoices; clean human-handoff on exceptions.

---

## Verdict on William's Existing Stack

| Component | Verdict | Rationale |
|---|---|---|
| **CrewAI** | KEEP — as orchestration front door | Right mental model for role-defined back-office agents; native MCP + A2A; massive adoption; pairs cleanly with LangGraph. |
| **scotthavird/crewai-template** | KEEP as scaffolding only | Useful for project structure; do not let it dictate architecture. Treat as starter, not framework. |
| **agno-agi/agno** | EVALUATE LATER, do not adopt now | Agno positions itself as a framework-agnostic runtime (AgentOS) atop CrewAI / LangGraph / DSPy. Interesting but redundant when you're already on K8s + Langfuse + Postgres. Revisit at Month 6 if you outgrow your runtime layer. Not foundational. |
| **anthropics/financial-services** | KEEP as reference, not dependency | Excellent FDE-pattern reference for how Anthropic structures multi-agent financial workflows. Mine it for patterns; do not vendor the code. |

---

## Anti-Recommendations (Do Not Build On These)

| Project | Why avoid |
|---|---|
| **AutoGPT** | Demo-grade. No production deployments at scale. Architectural dead end. |
| **BabyAGI** | Toy project. Abandoned trajectory. Conceptually superseded by every framework above. |
| **OpenAI Swarm** | Officially superseded by OpenAI Agents SDK (March 2025). If you want OpenAI-native, use Agents SDK — but you don't, because of EU sovereignty. |
| **Semantic Kernel (standalone)** | Merged into Microsoft Agent Framework (April 2026). Don't start new builds on standalone SK. |
| **Llama Agents (Meta)** | Sparse maintenance; LlamaIndex is the active project. |
| **MetaGPT / ChatDev / AgentVerse** | Research-grade. Beautiful papers, no production track record. |
| **Swarms (kyegomez)** | Controversial maintainer signal; community trust is mixed; substantial code-quality concerns flagged by senior practitioners. Skip. |
| **HuggingFace Smolagents** | Excellent for tiny demos and education. Wrong abstraction for a Palantir-grade build. |
| **Atomic Agents / Pydantic AI (as core orchestrator)** | Type-safe and elegant; but neither has the durable-execution story LangGraph offers. Use Pydantic AI inside DSPy signatures, not as orchestrator. |
| **Anything claiming "AGI" in its README** | Founder selection signal. |

---

## The Strategic Read

You're not building a chatbot company. You're building the operating system for Mittelstand back-office automation — which means three things matter more than feature lists:

1. **Sovereignty.** Frankfurt or nothing. Mistral is your only frontier-class lab inside EU jurisdiction; treat them as a strategic partner, not a vendor. Self-host the open-weights everything-else (Qwen, Llama, DeepSeek, Granite).

2. **Compilability.** The half-life of an LLM in 2026 is ~90 days. The teams that win are the ones who can re-compile their stack against a new model in an afternoon. DSPy + Langfuse + LiteLLM is that loop. Hand-tuned prompts are the path to irrelevance by Q3.

3. **Compounding observability.** Putsch's competitive advantage compounds the moment every invoice processed becomes a labeled eval example. Langfuse + DSPy + GEPA is the flywheel. Get it spinning in the first 30 days, and by Month 6 you have a German back-office eval corpus that no competitor — not SAP, not DATEV, not the big four consultancies — can replicate without your data.

**Build to MCP and A2A from day one.** Both are Linux Foundation standards now, governed by AAIF with six major AI labs as co-founders. Anything that doesn't speak both protocols is technical debt the day you commit it.

This is the stack. Ship it.
