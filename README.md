# Agentic Labor

EU-sovereign, Frankfurt-hosted multi-agent back-office automation for Putsch Mittelstand.

LangGraph durable orchestration over CrewAI / Magentic-One crews, DSPy-compiled task modules, MCP tool servers (SAP, DATEV, Docling, Atlas), and an LLM router (LiteLLM) fronting a vLLM cluster of open-weights models plus Mistral La Plateforme as the EU-jurisdiction frontier fallback.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full design, deployment topology, LLM-to-task model matrix, 30-day stand-up plan, stack verdicts, and anti-recommendations.

## Stack at a glance

- **Orchestration:** LangGraph (Postgres checkpointer, HITL via `interrupt()`)
- **Crews:** CrewAI (AP, Sales) + Magentic-One pattern (Customs / Master Data)
- **Task modules:** DSPy (GEPA-optimized), Pydantic signatures
- **Tools:** MCP servers — SAP, DATEV, Docling, Atlas (EZT-Online), Email, SharePoint
- **Memory:** Zep + Graphiti on Neo4j
- **Routing:** LiteLLM → vLLM (Mistral Large 2, Qwen2.5-72B, Qwen2.5-Coder-32B, Llama 3.3 70B, Granite-Docling 258M)
- **Frontier fallback:** Mistral La Plateforme (Paris, EU jurisdiction)
- **Observability:** Langfuse (OTel → Postgres + ClickHouse)
- **Identity:** Keycloak, OAuth 2.1 + PKCE on all MCP connections
- **Region:** AWS Frankfurt (`eu-central-1`) or Hetzner Falkenstein / Nuremberg
