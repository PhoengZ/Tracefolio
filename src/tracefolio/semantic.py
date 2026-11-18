import ast
import re
from functools import lru_cache

from .contracts.packets import SemanticFileEntry
from .privacy import path_allowed, sanitize_text

_GO_DECL = re.compile(
    r"^\s*(?:package\s+([A-Za-z_]\w*)|type\s+([A-Za-z_]\w*)\s+(struct|interface)\b|"
    r"func\s+(?:\([^)]+\)\s*)?([A-Za-z_]\w*)\s*\()",
    re.MULTILINE,
)
_TS_DECL = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+function|function|class|interface|type|const)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
_JAVA_DECL = re.compile(
    r"^\s*(?:package\s+([\w.]+);|(?:public|protected|private)?\s*(?:static\s+|final\s+|abstract\s+)*"
    r"(class|interface|enum|record)\s+([A-Za-z_]\w*)|"
    r"(?:public|protected)\s+(?:static\s+|final\s+)*[\w<>[\], ?]+\s+([A-Za-z_]\w*)\s*\()",
    re.MULTILINE,
)
_PROTO_DECL = re.compile(
    r"^\s*(?:package\s+([\w.]+);|service\s+([A-Za-z_]\w*)|rpc\s+([A-Za-z_]\w*)\s*\(|message\s+([A-Za-z_]\w*))",
    re.MULTILINE,
)
_SQL_DECL = re.compile(
    r"\b(?:CREATE|ALTER)\s+(?:OR\s+REPLACE\s+)?(TABLE|VIEW|INDEX|TRIGGER|FUNCTION)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_.\"]+)",
    re.IGNORECASE,
)
_YAML_DECL = re.compile(
    r"^\s*(?:operationId:\s*([A-Za-z0-9_.-]+)|(/[A-Za-z0-9_./{}-]+):\s*$)",
    re.MULTILINE,
)
_MD_HEADING = re.compile(r"^\s*(#{1,3}\s+.+)$", re.MULTILINE)
_PY_FALLBACK = re.compile(
    r"^\s*(?:(class)\s+([A-Za-z_]\w*)|(?:async\s+)?(def)\s+([A-Za-z_]\w*)\s*\()",
    re.MULTILINE,
)


def detect_file_kind(path: str | None) -> str:
    if not path:
        return "unknown"
    lower = path.lower()
    if lower.endswith(".py"):
        return "python"
    if lower.endswith(".ipynb"):
        return "notebook"
    if lower.endswith(".go"):
        return "go"
    if lower.endswith((".ts", ".tsx")):
        return "typescript"
    if lower.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "javascript"
    if lower.endswith(".java"):
        return "java"
    if lower.endswith(".proto"):
        return "protobuf"
    if lower.endswith((".yaml", ".yml")):
        return "yaml"
    if lower.endswith(".sql"):
        return "sql"
    if lower.endswith(".md"):
        return "markdown"
    if lower.endswith(".json"):
        return "json"
    return "text"


def _clean_code_text(text: str) -> str:
    lines = text.splitlines()
    if lines and lines[0].startswith(("commit: ", "merge_context: ")):
        cleaned = []
        for line in lines[2:]:
            if line.startswith(("+++", "---", "@@")):
                continue
            if line.startswith("+"):
                cleaned.append(line[1:])
            elif not line.startswith("-"):
                cleaned.append(line)
        return "\n".join(cleaned)
    return text


def _python_symbols(code: str, max_symbols: int) -> list[str]:
    symbols = []
    try:
        tree = ast.parse(code)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = [
                    item.name
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and (not item.name.startswith("_") or item.name == "__init__")
                ]
                suffix = f" ({', '.join(methods[:6])})" if methods else ""
                symbols.append(f"class {node.name}{suffix}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args if a.arg != "self"][:5]
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                symbols.append(f"{prefix} {node.name}({', '.join(args)})")
            if len(symbols) >= max_symbols:
                break
    except SyntaxError:
        for match in _PY_FALLBACK.finditer(code):
            if match.group(1) == "class":
                symbols.append(f"class {match.group(2)}")
            else:
                symbols.append(f"def {match.group(4)}")
            if len(symbols) >= max_symbols:
                break
    return symbols[:max_symbols]


@lru_cache(maxsize=4096)
def extract_semantic_symbols(path: str | None, payload: str, max_symbols: int = 30) -> tuple[str, list[str]]:
    kind = detect_file_kind(path)
    code = _clean_code_text(payload or "")
    symbols: list[str] = []
    if kind in {"python", "notebook"}:
        symbols = _python_symbols(code, max_symbols)
        if kind == "notebook" and len(symbols) < max_symbols:
            for match in _MD_HEADING.finditer(code):
                heading = match.group(1).strip()
                if heading not in symbols:
                    symbols.append(heading[:80])
                if len(symbols) >= max_symbols:
                    break
    elif kind == "go":
        for m in _GO_DECL.finditer(code):
            if m.group(1):
                sym = f"package {m.group(1)}"
            elif m.group(2):
                sym = f"type {m.group(2)} {m.group(3)}"
            else:
                sym = f"func {m.group(4)}"
            if sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= max_symbols:
                break
    elif kind in {"typescript", "javascript"}:
        for m in _TS_DECL.finditer(code):
            sym = m.group(1)
            if sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= max_symbols:
                break
    elif kind == "java":
        for m in _JAVA_DECL.finditer(code):
            if m.group(1):
                sym = f"package {m.group(1)}"
            elif m.group(3):
                sym = f"{m.group(2)} {m.group(3)}"
            else:
                sym = f"method {m.group(4)}"
            if sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= max_symbols:
                break
    elif kind == "protobuf":
        for m in _PROTO_DECL.finditer(code):
            sym = (
                f"package {m.group(1)}"
                if m.group(1)
                else f"service {m.group(2)}"
                if m.group(2)
                else f"rpc {m.group(3)}"
                if m.group(3)
                else f"message {m.group(4)}"
            )
            if sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= max_symbols:
                break
    elif kind == "sql":
        for m in _SQL_DECL.finditer(code):
            sym = f"{m.group(1).upper()} {m.group(2)}"
            if sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= max_symbols:
                break
    elif kind == "yaml":
        for m in _YAML_DECL.finditer(code):
            sym = f"operation {m.group(1)}" if m.group(1) else f"path {m.group(2)}"
            if sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= max_symbols:
                break
    elif kind == "markdown":
        for m in _MD_HEADING.finditer(code):
            sym = m.group(1).strip()[:80]
            if sym not in symbols:
                symbols.append(sym)
            if len(symbols) >= max_symbols:
                break
    return kind, symbols[:max_symbols]


def semantic_outline_payload(path: str, text: str, max_bytes: int) -> str:
    kind, symbols = extract_semantic_symbols(path, text, max_symbols=50)
    header_lines = [
        f"[semantic_outline] kind={kind} original_bytes={len(text.encode('utf-8'))}",
    ]
    if symbols:
        header_lines.append("symbols: " + ", ".join(symbols))
    header = "\n".join(header_lines) + "\n---\n"
    budget = max(64, max_bytes - len(header.encode("utf-8")) - 32)
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return header + text
    half = max(32, budget // 2)
    head = encoded[:half].decode("utf-8", errors="ignore")
    tail = encoded[-half:].decode("utf-8", errors="ignore")
    return f"{header}{head}\n...[truncated]...\n{tail}"


def build_semantic_map(project, records, policy) -> list[SemanticFileEntry]:
    by_key = {}
    for record in records:
        if not record.source_relative_path:
            continue
        by_key[(record.source_id, record.source_relative_path)] = record

    entries: list[SemanticFileEntry] = []
    for (source_id, rel_path), record in sorted(by_key.items()):
        source_cfg = next((s for s in project.sources if s.source_id == source_id), None)
        work_ids = []
        if source_cfg:
            for unit in source_cfg.work_units:
                if path_allowed(rel_path, unit.include_paths, []):
                    work_ids.append(unit.work_id)
        is_deleted = record.revision.startswith("deleted-") or (
            record.payload.strip() == "File deleted from source snapshot."
        )
        kind, raw_symbols = extract_semantic_symbols(rel_path, record.payload)
        clean_symbols = [
            sanitize_text(sym, policy).text
            for sym in raw_symbols
            if sanitize_text(sym, policy).text
        ]
        entries.append(
            SemanticFileEntry(
                source_id=source_id,
                path=sanitize_text(rel_path, policy).text,
                kind=kind,
                work_ids=sorted(set(work_ids)),
                status="deleted" if is_deleted else "active",
                symbols=clean_symbols,
            )
        )
    return entries
