"""End-to-end demo: run a Mahnverfahren customs case through the swarm.

A single overdue invoice at Putsch touches procurement (PO match), logistics
(customs/HS-code), finance (AP open item), and DATEV (booking proposal).
The Magentic-One Orchestrator plans the case, dispatches each specialist,
and synthesizes a recommendation before the third dunning notice goes out.

Wire this with a real model factory before running. The factory here uses
LangChain's OpenAI provider as a placeholder; the Prompt-1 model gateway
should replace it.
"""

from __future__ import annotations

import argparse

from swarm import (
    Orchestrator,
    build_graph,
    build_putsch_registry,
    putsch_routing,
)


def langchain_factory(model_id: str):
    """Build a ChatModel handle for the given model id.

    Falls back to ChatOpenAI for any id; real deployments should route by
    prefix (e.g. mistral-*, qwen-*, granite-*) through the appropriate
    provider in the Prompt-1 model gateway.
    """

    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    class _Adapter:
        def __init__(self, model_id: str) -> None:
            self._llm = ChatOpenAI(model=model_id, temperature=0)

        def invoke(self, messages):
            converted = []
            for m in messages:
                role, content = m["role"], m["content"]
                if role == "system":
                    converted.append(SystemMessage(content=content))
                elif role == "assistant":
                    converted.append(AIMessage(content=content))
                else:
                    converted.append(HumanMessage(content=content))
            return self._llm.invoke(converted).content

    return _Adapter(model_id)


CUSTOMS_TASK = (
    "Eingangsrechnung 2025-04-1187 vom Lieferanten DE842791 ist seit 32 Tagen "
    "ueberfaellig. Vor der dritten Mahnung pruefen: wurde die Ware vollstaendig "
    "verzollt geliefert, stimmt die Rechnungssumme zum PO 4500017722 ueberein, "
    "und ist die USt-IdNr. des Lieferanten gueltig? Wenn alles passt, einen "
    "DATEV-Buchungsvorschlag erzeugen."
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=CUSTOMS_TASK, help="Task to run.")
    parser.add_argument(
        "--max-replans", type=int, default=2, help="Outer-loop replan cap."
    )
    args = parser.parse_args()

    if build_graph is None:
        raise RuntimeError("langgraph not installed; pip install langgraph")

    router = putsch_routing(langchain_factory)
    registry = build_putsch_registry()
    orchestrator = Orchestrator(
        router=router, workers=registry, max_replans=args.max_replans
    )
    graph = build_graph(orchestrator)

    final = graph.invoke({"task": args.task, "transcript": []})
    print(final.get("final_answer", "(no answer produced)"))


if __name__ == "__main__":
    main()
