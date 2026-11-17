import hashlib
import json
from dataclasses import dataclass

from ..errors import TracefolioError


@dataclass(frozen=True)
class NotebookSource:
    text: str
    cells: list[dict]


def normalize_notebook(raw_json: str) -> NotebookSource:
    try:
        doc = json.loads(raw_json)
        if not isinstance(doc, dict) or not isinstance(doc.get("cells"), list):
            raise TypeError
        texts, cells = [], []
        for index, cell in enumerate(doc["cells"]):
            if not isinstance(cell, dict):
                raise TypeError
            kind = cell.get("cell_type")
            if kind not in {"code", "markdown"}:
                continue
            source = cell.get("source", [])
            if isinstance(source, list) and all(isinstance(s, str) for s in source):
                source = "".join(source)
            if not isinstance(source, str):
                raise TypeError
            hashed = hashlib.sha256(source.encode("utf-8")).hexdigest()
            cells.append({"cell_index": index, "cell_type": kind, "source_digest": hashed})
            texts.append(f"# notebook-source-v1 cell {index} ({kind})\n{source}\n")
        return NotebookSource("\n".join(texts), cells)
    except (ValueError, TypeError, KeyError):
        raise TracefolioError("invalid_notebook", exit_code=3) from None


def normalize_text(raw: bytes, path: str, limits) -> tuple[str, dict]:
    is_notebook = path.lower().endswith(".ipynb")
    maximum = limits.max_notebook_bytes if is_notebook else limits.max_text_file_bytes
    if len(raw) > maximum or b"\0" in raw:
        raise TracefolioError("content_budget_or_binary", exit_code=3)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise TracefolioError("non_utf8_content", exit_code=3) from None
    metadata = {}
    if is_notebook:
        source = normalize_notebook(text)
        text = source.text
        metadata = {"normalization": "notebook-source-v1", "cells": source.cells}
    if len(text.encode()) > limits.max_text_file_bytes:
        raise TracefolioError("normalized_text_budget", exit_code=3)
    return text, metadata

