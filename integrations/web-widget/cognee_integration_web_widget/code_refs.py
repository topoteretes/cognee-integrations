"""Code from the ingested repository that a visitor's message points at.

cognee's code graph answers deterministic lookups of one exact, unique name -
a class, ``Class.method``, ``path/module.function`` or a file - and nothing for
a sentence. So instead of searching with the message, the names in it are
picked out (the identifiers people paste: class names, dotted module paths from
a traceback, file paths) and each is looked up on its own. What resolves comes
back as a "Related code" reference: the symbol, its file and line, and a link
when the repository's browse URL is configured.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, List, Optional

MAX_SEEDS = 3

# A .py path; inside a traceback or site-packages path, the part from the
# "cognee/" package directory on is what the code graph knows the file by.
_PY_PATH = re.compile(r"[\w./\\-]*\.py\b")
# The package directory, not a folder that merely ends in "cognee".
_PACKAGE_DIR = re.compile(r"(?:^|/)(cognee/[\w./-]+\.py)$")
# Interpreter and third-party files: a traceback is full of them, and the code
# graph holds only the ingested repository.
_FOREIGN = re.compile(r"/(?:site-packages|dist-packages)/(?!cognee/)|/lib/python\d")
# A traceback frame names the function as well as the file.
_FRAME = re.compile(r'File "([^"]+\.py)", line \d+, in ([A-Za-z_]\w*)')
_ERROR_NAME = re.compile(r"(?:Error|Exception|Warning)$")
# A dotted name: module paths in logs, Class.method, package.attr. Domains and
# file names are left to the other rules.
_DOTTED = re.compile(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b")
# A CamelCase name with at least two humps: SearchType, NativeLiteLLMAdapter.
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+\b")
_DOMAIN_PART = {"com", "ai", "io", "org", "net", "dev", "www", "http", "https"}
# Dotted names that are file names, not code: Activate.ps1, README.md.
_FILE_SUFFIX = {"ps1", "md", "mdx", "json", "yml", "yaml", "toml", "txt", "sh", "js", "ts", "env"}
# Capitalised words people type that are products or Python builtins, never
# something the ingested repository defines.
_NOT_CODE = {
    "GitHub", "GitLab", "OpenAI", "ChatGPT", "MiniLM", "NoneType", "Info", "PyPI",
    "JavaScript", "TypeScript", "LinkedIn", "YouTube", "MacOS", "PostgreSQL", "MySQL",
}  # fmt: skip

# cognee's public operations. Each is defined at cognee/api/v1/<name>/<name>.py
# under its own name, which is how the code graph resolves it. They are also
# ordinary words ("I remember...", "add a file"), so one counts only when the
# message plainly means the operation: right after "cognee" ("cognee remember",
# cognee.recall), written as a call (forget(...)) or in backticks (`improve`).
_API_OPERATIONS = {"add", "cognify", "search", "remember", "recall", "forget", "improve", "serve"}
_API_MENTION = re.compile(
    r"\bcognee(?:'s)?[ .]+(\w+)|\b(\w+)\(|`(\w+)(?:\(\))?`",
    re.I,
)


def _is_camel(part: str) -> bool:
    return bool(_CAMEL.fullmatch(part)) or (part[:1].isupper() and part[1:2].islower())


def _class_seed(dotted: str) -> Optional[str]:
    """``a.b.Class.method`` -> ``Class.method``; ``a.Class`` -> ``Class``; else None.

    The last class in the chain names the code. A method name after it is kept;
    a constant (``SearchType.GRAPH_COMPLETION``) is not something the code graph
    defines, so the class stands alone.
    """
    parts = dotted.split(".")
    classes = [i for i, p in enumerate(parts) if _is_camel(p)]
    if not classes:
        return None
    i = classes[-1]
    member = parts[i + 1] if i + 1 < len(parts) else ""
    return f"{parts[i]}.{member}" if member[:1].islower() else parts[i]


def _package_path(raw: str) -> Optional[str]:
    """``.../site-packages/cognee/a/b.py`` -> ``cognee/a/b.py``; None outside cognee."""
    path = raw.replace("\\", "/")
    if "site-packages/" in path:
        path = path[path.rfind("site-packages/") + len("site-packages/") - 1 :]
    inside = _PACKAGE_DIR.search(path)
    return inside.group(1) if inside else None


def extract_code_seeds(message: str, limit: int = MAX_SEEDS) -> List[str]:
    """Up to ``limit`` names in ``message`` worth looking up, most telling first.

    In order: where in cognee it happened (the function of the innermost cognee
    frame of a traceback, a cognee module path in a log line), the exception
    raised, other class names, then the remaining cognee files and the user's
    own files.
    """
    # A traceback reads outermost first; the frame that raised is the last one.
    frames: List[str] = []
    for raw, function in reversed(_FRAME.findall(message)):
        path = _package_path(raw)
        if path:
            # The code graph names a function by its module path: a/b.function.
            frames.append(f"{path[: -len('.py')]}.{function}")

    package_paths: List[str] = []
    other_files: List[str] = []
    for raw in _PY_PATH.findall(message):
        path = _package_path(raw)
        if path:
            package_paths.append(path)
        elif not _FOREIGN.search(raw.replace("\\", "/")):
            other_files.append(raw.replace("\\", "/").rsplit("/", 1)[-1])
    package_paths.reverse()

    cognee_dotted, other_dotted = [], []
    for dotted in _DOTTED.findall(message):
        parts = dotted.split(".")
        if parts[-1] in ("py", *_FILE_SUFFIX) or _DOMAIN_PART & {p.lower() for p in parts}:
            continue
        seed = _class_seed(dotted)
        if seed and seed.split(".")[0] not in _NOT_CODE:
            (cognee_dotted if parts[0] == "cognee" else other_dotted).append(seed)

    operations = []
    for groups in _API_MENTION.findall(message):
        name = next((g for g in groups if g), "").lower()
        if name in _API_OPERATIONS:
            operations.append(f"cognee/api/v1/{name}/{name}.{name}")

    camel = [n for n in _CAMEL.findall(message) if n not in _NOT_CODE]
    errors = [n for n in camel if _ERROR_NAME.search(n)]
    others = [n for n in camel if not _ERROR_NAME.search(n)]

    seeds: List[str] = []
    for group in (
        # The function that raised says more than its file; the file stands in
        # when the frame named none.
        frames[:1] or package_paths[:1],
        cognee_dotted,
        operations,
        errors,
        other_dotted,
        others,
        frames[1:],
        package_paths,
        other_files,
    ):
        for seed in group:
            # A class already looked up as Class.method needs no lookup of its own.
            if seed not in seeds and not any(seed in s.split(".") for s in seeds if "." in s):
                seeds.append(seed)
    return seeds[:limit]


@dataclasses.dataclass(frozen=True)
class CodeRef:
    """One code-graph node the message pointed at."""

    name: str
    symbol: str
    kind: str
    file: str
    line: Optional[int]
    url: Optional[str]

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _short_name(symbol: str, kind: str) -> str:
    """``.../native_adapter.NativeLiteLLMAdapter.acreate`` -> ``NativeLiteLLMAdapter.acreate``."""
    if kind == "CodeFileReference":
        return symbol.rsplit("/", 1)[-1]
    tail = symbol.rsplit("/", 1)[-1]
    return tail.split(".", 1)[1] if "." in tail else tail


def code_url(file: str, line: Optional[int], base_url: Optional[str]) -> Optional[str]:
    if not base_url or not file:
        return None
    url = base_url.rstrip("/") + "/" + file.lstrip("/")
    return f"{url}#L{line}" if line else url


def _payload(entry: Any) -> Optional[dict]:
    """The decoded code-graph result of a recall entry, or ``None``."""
    text = entry.get("text") if isinstance(entry, dict) else getattr(entry, "text", None)
    try:
        payload = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_code_entry(entry: Any, base_url: Optional[str] = None) -> Optional[CodeRef]:
    """The focus node of a code-graph result, or ``None`` if it carries none."""
    payload = _payload(entry)
    focus = payload.get("focus") if payload else None
    if not isinstance(focus, dict) or not focus.get("name"):
        return None
    symbol = str(focus["name"])
    kind = str(focus.get("type") or "")
    file = str(focus.get("file") or "")
    line = focus.get("line") if isinstance(focus.get("line"), int) else None
    return CodeRef(
        name=_short_name(symbol, kind),
        symbol=symbol,
        kind=kind,
        file=file,
        line=line,
        url=code_url(file, line, base_url),
    )


# What the answering model is told about the code a message names, so it can
# tell which part of cognee is involved. Structure only: the code graph holds
# no source, so the block says so rather than letting the model guess bodies.
CODE_CONTEXT_HEADER = (
    "Code in the cognee repository that the user's message refers to, from cognee's "
    "code graph: what each item is, where it is defined, what calls it and what it "
    "calls. It is structure, not source code. Use it to tell which part of cognee is "
    "involved; you may name these items, but do not describe code beyond what is listed."
)
MAX_RELATED = 6
MAX_CODE_CONTEXT_CHARS = 2500


def _short_target(symbol: str) -> str:
    return _short_name(symbol, "CodeFileReference" if symbol.endswith(".py") else "")


def _describe(payload: dict) -> Optional[str]:
    focus = payload.get("focus")
    if not isinstance(focus, dict) or not focus.get("name"):
        return None
    symbol, kind = str(focus["name"]), str(focus.get("type") or "")
    what = focus.get("symbol_kind") or ("file" if kind == "CodeFileReference" else kind)
    where = f"{focus.get('file')}:{focus['line']}" if focus.get("line") else focus.get("file")
    lines = [f"- {_short_name(symbol, kind)} ({what}), defined in {where}"]

    relations = focus.get("relations") or []
    calls = [r["target"] for r in relations if r.get("type") == "calls"]
    # What a function instantiates is usually the exception it raises.
    creates = [r["target"] for r in relations if r.get("type") == "instantiates"]
    # Tests calling it say nothing about how a user reaches it.
    callers = [
        e["source"]
        for e in payload.get("edges") or []
        if e.get("type") == "calls"
        and e.get("target_id") == focus.get("id")
        and "/tests/" not in str(e.get("source"))
    ]
    for label, names in (("called by", callers), ("calls", calls), ("creates", creates)):
        short = list(dict.fromkeys(_short_target(n) for n in names if n))
        if short:
            more = f" (+{len(short) - MAX_RELATED} more)" if len(short) > MAX_RELATED else ""
            lines.append(f"  {label}: {', '.join(short[:MAX_RELATED])}{more}")
    return "\n".join(lines)


def code_context_block(entries: List[Any]) -> str:
    """A short system-prompt block describing the looked-up code, or ``""``."""
    items, seen = [], set()
    for entry in entries:
        payload = _payload(entry)
        focus = payload.get("focus") if payload else None
        if not isinstance(focus, dict) or focus.get("name") in seen:
            continue
        described = _describe(payload)
        if described:
            seen.add(focus.get("name"))
            items.append(described)
    if not items:
        return ""
    block = CODE_CONTEXT_HEADER
    for item in items:
        if len(block) + len(item) + 1 > MAX_CODE_CONTEXT_CHARS:
            break
        block += "\n" + item
    return block
