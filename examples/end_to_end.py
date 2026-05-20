"""End-to-end demo: Stammdaten lookup -> Magentic-One swarm.

Wires the full chain a customs/Mahnverfahren case follows:
  1. StammdatenCrew resolves the vendor by USt-IdNr.
  2. SwarmCrew (the Magentic-One graph) runs the multi-specialist
     investigation, using the Stammdaten output as wrapped context.

The model factory here uses LangChain's OpenAI provider; the Prompt-1
model gateway will replace it once that module lands.
"""

from __future__ import annotations

import argparse

from crews import StammdatenCrew, SwarmCrew
from harness import Pipeline
from swarm import Orchestrator, build_putsch_registry, putsch_routing


def langchain_factory(model_id: str):
    """Return a ChatModel adapter for the given identifier.

    The Prompt-1 model gateway replaces this with provider-aware routing.
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


VENDORS = {
    "DE842791": {
        "name": "MusterLieferant GmbH",
        "iban": "DE89 3704 0044 0532 0130 00",
        "country": "DE",
        "payment_terms": "30 Tage netto",
    },
}


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
    args = parser.parse_args()

    router = putsch_routing(langchain_factory)
    registry = build_putsch_registry()
    orchestrator = Orchestrator(router=router, workers=registry)

    pipeline = Pipeline([
        StammdatenCrew(vendors=VENDORS),
        SwarmCrew(orchestrator=orchestrator),
    ])

    result = pipeline.run(args.task)
    print(f"Crews executed: {', '.join(pipeline.crew_names)}")
    print()
    print("Final summary:")
    print(result.final_summary)


if __name__ == "__main__":
    main()
