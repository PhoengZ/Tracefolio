import argparse
import json
from pathlib import Path

from .contracts.evidence import CollectionManifest, EvidenceRecord
from .contracts.knowledge import ProjectRecord, WorkRecord
from .contracts.packets import AnalysisProposal, InvestigationPacket
from .contracts.portfolio import PortfolioSnapshot
from .contracts.routes import RouteRegistry

CONTRACTS = {
    "routes": RouteRegistry, "evidence": EvidenceRecord, "collection": CollectionManifest,
    "packet": InvestigationPacket, "proposal": AnalysisProposal,
    "project": ProjectRecord, "work": WorkRecord, "portfolio": PortfolioSnapshot,
}


def schemas() -> dict:
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema",
                   **model.model_json_schema()} for name, model in CONTRACTS.items()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--directory", type=Path, default=Path("schemas"))
    args = parser.parse_args(argv)
    for name, schema in schemas().items():
        path = args.directory / f"{name}.schema.json"
        text = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                return 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

