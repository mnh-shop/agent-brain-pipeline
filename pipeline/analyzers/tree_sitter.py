from __future__ import annotations

import ast
import importlib.metadata
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml
from charset_normalizer import from_bytes

from pipeline.schemas.ids import normalize_path, stable_hash, symbol_identity
from pipeline.util import sha256_bytes, sha256_text

try:  # pragma: no cover - optional runtime dependency
    from tree_sitter_language_pack import get_parser  # type: ignore
except Exception:  # pragma: no cover - package not installed in this environment
    get_parser = None  # type: ignore


LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
}

SUPPORTED_LANGUAGES = {"python", "javascript", "typescript", "json", "yaml", "toml", "shell"}
CONFIG_LANGUAGES = {"json", "yaml", "toml"}
FALLBACK_LANGUAGES = {"markdown", "text", "rst", "shell"}


@dataclass(frozen=True)
class SyntaxEntry:
    symbol: dict[str, Any] | None = None
    import_record: dict[str, Any] | None = None
    node: dict[str, Any] | None = None


@dataclass(frozen=True)
class SyntaxAnalysis:
    path: str
    language: str
    tree_sitter_available: bool
    tree_sitter_version: str | None
    parser_name: str
    parser_version: str
    parse_errors: list[dict[str, Any]] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    imports: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[dict[str, Any]] = field(default_factory=list)
    fallback_units: list[dict[str, Any]] = field(default_factory=list)
    unit_kind: str | None = None
    passed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "language": self.language,
            "tree_sitter_available": self.tree_sitter_available,
            "tree_sitter_version": self.tree_sitter_version,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "parse_errors": self.parse_errors,
            "symbols": self.symbols,
            "imports": self.imports,
            "nodes": self.nodes,
            "fallback_units": self.fallback_units,
            "unit_kind": self.unit_kind,
            "passed": self.passed,
        }


def tree_sitter_package_version() -> str | None:
    if get_parser is None:
        return None
    try:
        return importlib.metadata.version("tree-sitter-language-pack")
    except importlib.metadata.PackageNotFoundError:
        try:
            return importlib.metadata.version("tree_sitter_language_pack")
        except importlib.metadata.PackageNotFoundError:
            return None


def detect_language(path: Path) -> str:
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "unsupported")


def _line_offsets(text: str) -> list[tuple[int, int]]:
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        data = line.encode("utf-8")
        offsets.append((cursor, cursor + len(data)))
        cursor += len(data)
    if not offsets and text:
        data = text.encode("utf-8")
        offsets.append((0, len(data)))
    return offsets


def _byte_span_from_lines(text: str, start_line: int, end_line: int) -> tuple[int, int]:
    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(text)
    start_index = max(1, start_line) - 1
    end_index = max(1, end_line) - 1
    start_byte = offsets[start_index][0] if offsets else 0
    end_byte = offsets[end_index][1] if offsets else len(text.encode("utf-8"))
    return start_byte, end_byte


def _slice_lines(text: str, start_line: int, end_line: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[start_line - 1 : end_line])


def _module_name(path: str) -> str:
    normalized = normalize_path(path)
    return normalized.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".").strip(".") or "module"


def _make_symbol(
    metadata: dict[str, Any],
    path: str,
    language: str,
    symbol_kind: str,
    qualified_name: str,
    start_line: int,
    end_line: int,
    start_byte: int,
    end_byte: int,
    content: str,
    *,
    start_byte_override: int | None = None,
    end_byte_override: int | None = None,
    generator_name: str = "syntax:tree-sitter",
    generator_version: str = "1",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_path(path)
    content_sha256 = sha256_text(content)
    symbol = {
        "symbol_id": symbol_identity(
            metadata["source_id"],
            metadata["commit_sha"],
            normalized,
            language,
            symbol_kind,
            qualified_name,
            start_byte_override if start_byte_override is not None else start_byte,
            end_byte_override if end_byte_override is not None else end_byte,
            content_sha256,
        ),
        "source_id": metadata["source_id"],
        "platform": metadata["platform"],
        "repository_url": metadata["repository_url"],
        "namespace": metadata["namespace"],
        "repository_name": metadata["repository_name"],
        "requested_ref": metadata.get("requested_ref"),
        "resolved_branch": metadata.get("resolved_branch"),
        "commit_sha": metadata["commit_sha"],
        "path": path,
        "normalized_path": normalized,
        "language": language,
        "symbol_kind": symbol_kind,
        "qualified_name": qualified_name,
        "start_byte": start_byte_override if start_byte_override is not None else start_byte,
        "end_byte": end_byte_override if end_byte_override is not None else end_byte,
        "start_line": start_line,
        "end_line": end_line,
        "file_sha256": metadata["file_sha256"],
        "content_sha256": content_sha256,
        "content": content,
        "generator_name": generator_name,
        "generator_version": generator_version,
        "schema_version": 1,
        "pipeline_version": metadata["pipeline_version"],
        "run_id": metadata.get("run_id"),
    }
    if extra:
        symbol.update(extra)
    return symbol


def _make_import(
    metadata: dict[str, Any],
    path: str,
    language: str,
    import_kind: str,
    imported_name: str,
    start_line: int,
    end_line: int,
    start_byte: int,
    end_byte: int,
    content: str,
    imported_as: str | None = None,
) -> dict[str, Any]:
    normalized = normalize_path(path)
    content_sha256 = sha256_text(content)
    return {
        "import_id": stable_hash(
            metadata["source_id"],
            metadata["commit_sha"],
            normalized,
            language,
            import_kind,
            imported_name,
            imported_as or "",
            start_byte,
            end_byte,
            content_sha256,
        ),
        "source_id": metadata["source_id"],
        "platform": metadata["platform"],
        "repository_url": metadata["repository_url"],
        "namespace": metadata["namespace"],
        "repository_name": metadata["repository_name"],
        "requested_ref": metadata.get("requested_ref"),
        "resolved_branch": metadata.get("resolved_branch"),
        "commit_sha": metadata["commit_sha"],
        "path": path,
        "normalized_path": normalized,
        "language": language,
        "import_kind": import_kind,
        "imported_name": imported_name,
        "imported_as": imported_as,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "start_line": start_line,
        "end_line": end_line,
        "file_sha256": metadata["file_sha256"],
        "content_sha256": content_sha256,
        "content": content,
        "generator_name": "syntax:tree-sitter",
        "generator_version": "1",
        "schema_version": 1,
        "pipeline_version": metadata["pipeline_version"],
    }


def _make_node(
    kind: str,
    path: str,
    language: str,
    start_line: int,
    end_line: int,
    start_byte: int,
    end_byte: int,
    content: str,
    *,
    qualified_name: str | None = None,
    symbol_kind: str | None = None,
    node_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "node_id": stable_hash(path, language, kind, qualified_name or node_name or "", start_line, end_line, start_byte, end_byte, sha256_text(content)),
        "kind": kind,
        "path": path,
        "normalized_path": normalize_path(path),
        "language": language,
        "qualified_name": qualified_name,
        "symbol_kind": symbol_kind,
        "node_name": node_name,
        "start_line": start_line,
        "end_line": end_line,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "content_sha256": sha256_text(content),
    }
    if extra:
        payload.update(extra)
    return payload


def _collect_python(text: str, path: str, metadata: dict[str, Any], cfg: dict[str, Any]) -> SyntaxAnalysis:
    module_name = _module_name(path)
    offsets = _line_offsets(text)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return SyntaxAnalysis(
            path=path,
            language="python",
            tree_sitter_available=get_parser is not None,
            tree_sitter_version=tree_sitter_package_version(),
            parser_name="python-ast",
            parser_version="3.12",
            parse_errors=[{"kind": "syntax_error", "message": str(exc), "line": exc.lineno, "column": exc.offset}],
            fallback_units=[_fallback_unit(path, metadata, text, "python")],
            unit_kind="fallback",
            passed=False,
        )

    nodes: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    exported: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            start_line = node.lineno
            end_line = getattr(node, "end_lineno", node.lineno)
            start_byte, end_byte = _byte_span_from_lines(text, start_line, end_line)
            content = _slice_lines(text, start_line, end_line)
            kind = "import_from" if isinstance(node, ast.ImportFrom) else "import"
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_name = node.module
            else:
                imported_name = ", ".join(alias.name for alias in node.names)
            for alias in node.names:
                imports.append(
                    _make_import(
                        metadata,
                        path,
                        "python",
                        kind,
                        alias.name if isinstance(node, ast.ImportFrom) else alias.name,
                        start_line,
                        end_line,
                        start_byte,
                        end_byte,
                        content,
                        alias.asname,
                    )
                )
            nodes.append(_make_node("import", path, "python", start_line, end_line, start_byte, end_byte, content, node_name=imported_name))
        elif isinstance(node, ast.Assign):
            target_names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if target_names and any(name == "__all__" for name in target_names):
                try:
                    value = ast.literal_eval(node.value)
                    if isinstance(value, (list, tuple, set)):
                        exported.update(str(item) for item in value)
                except Exception:
                    pass

    def walk(body: Iterable[ast.stmt], prefix: str, owner: str | None = None) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start_line = node.lineno
                end_line = getattr(node, "end_lineno", node.lineno)
                start_byte, end_byte = _byte_span_from_lines(text, start_line, end_line)
                content = _slice_lines(text, start_line, end_line)
                kind = "constructor" if owner and node.name == "__init__" else ("method" if owner else "function")
                qualified_name = f"{prefix}.{node.name}" if prefix else node.name
                symbol = _make_symbol(
                    metadata,
                    path,
                    "python",
                    kind,
                    qualified_name,
                    start_line,
                    end_line,
                    start_byte,
                    end_byte,
                    content,
                    extra={"is_exported": node.name in exported, "owner": owner},
                )
                symbols.append(symbol)
                nodes.append(
                    _make_node(
                        kind,
                        path,
                        "python",
                        start_line,
                        end_line,
                        start_byte,
                        end_byte,
                        content,
                        qualified_name=qualified_name,
                        symbol_kind=kind,
                        node_name=node.name,
                    )
                )
            elif isinstance(node, ast.ClassDef):
                start_line = node.lineno
                end_line = getattr(node, "end_lineno", node.lineno)
                start_byte, end_byte = _byte_span_from_lines(text, start_line, end_line)
                content = _slice_lines(text, start_line, end_line)
                qualified_name = f"{prefix}.{node.name}" if prefix else node.name
                bases = [ast.unparse(base) for base in node.bases] if hasattr(ast, "unparse") else [getattr(base, "id", "") for base in node.bases]
                symbol = _make_symbol(
                    metadata,
                    path,
                    "python",
                    "class",
                    qualified_name,
                    start_line,
                    end_line,
                    start_byte,
                    end_byte,
                    content,
                    extra={"bases": bases, "is_exported": node.name in exported},
                )
                symbols.append(symbol)
                nodes.append(
                    _make_node(
                        "class",
                        path,
                        "python",
                        start_line,
                        end_line,
                        start_byte,
                        end_byte,
                        content,
                        qualified_name=qualified_name,
                        symbol_kind="class",
                        node_name=node.name,
                        extra={"bases": bases},
                    )
                )
                walk(node.body, qualified_name, owner=qualified_name)
            elif isinstance(node, ast.Assign):
                start_line = node.lineno
                end_line = getattr(node, "end_lineno", node.lineno)
                target_names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if target_names and all(name.replace("_", "").isupper() for name in target_names):
                    start_byte, end_byte = _byte_span_from_lines(text, start_line, end_line)
                    content = _slice_lines(text, start_line, end_line)
                    for target_name in target_names:
                        qualified_name = f"{prefix}.{target_name}" if prefix else target_name
                        symbol = _make_symbol(
                            metadata,
                            path,
                            "python",
                            "constant",
                            qualified_name,
                            start_line,
                            end_line,
                            start_byte,
                            end_byte,
                            content,
                            extra={"is_exported": target_name in exported},
                        )
                        symbols.append(symbol)
                        nodes.append(
                            _make_node(
                                "constant",
                                path,
                                "python",
                                start_line,
                                end_line,
                                start_byte,
                                end_byte,
                                content,
                                qualified_name=qualified_name,
                                symbol_kind="constant",
                                node_name=target_name,
                            )
                        )
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id.replace("_", "").isupper():
                start_line = node.lineno
                end_line = getattr(node, "end_lineno", node.lineno)
                start_byte, end_byte = _byte_span_from_lines(text, start_line, end_line)
                content = _slice_lines(text, start_line, end_line)
                qualified_name = f"{prefix}.{node.target.id}" if prefix else node.target.id
                symbol = _make_symbol(
                    metadata,
                    path,
                    "python",
                    "constant",
                    qualified_name,
                    start_line,
                    end_line,
                    start_byte,
                    end_byte,
                    content,
                )
                symbols.append(symbol)
                nodes.append(
                    _make_node(
                        "constant",
                        path,
                        "python",
                        start_line,
                        end_line,
                        start_byte,
                        end_byte,
                        content,
                        qualified_name=qualified_name,
                        symbol_kind="constant",
                        node_name=node.target.id,
                    )
                )

    walk(tree.body, module_name)
    module_end_line = len(text.splitlines()) or 1
    module_start_byte, module_end_byte = _byte_span_from_lines(text, 1, module_end_line)
    module_content = _slice_lines(text, 1, module_end_line) if text else ""
    module_symbol = _make_symbol(
        metadata,
        path,
        "python",
        "module",
        module_name,
        1,
        module_end_line,
        module_start_byte,
        module_end_byte,
        module_content,
    )
    symbols.insert(0, module_symbol)
    nodes.insert(0, _make_node("module", path, "python", 1, module_end_line, module_start_byte, module_end_byte, module_content, qualified_name=module_name, symbol_kind="module"))

    return SyntaxAnalysis(
        path=path,
        language="python",
        tree_sitter_available=get_parser is not None,
        tree_sitter_version=tree_sitter_package_version(),
        parser_name="python-ast",
        parser_version="3.12",
        parse_errors=parse_errors,
        symbols=symbols,
        imports=imports,
        nodes=nodes,
        unit_kind="symbol",
        passed=True,
    )


def _match_brace_block(text: str, start_line: int) -> int:
    lines = text.splitlines()
    depth = 0
    started = False
    for line_no in range(start_line - 1, len(lines)):
        line = lines[line_no]
        for char in line:
            if char == "{":
                depth += 1
                started = True
            elif char == "}":
                depth -= 1
                if started and depth <= 0:
                    return line_no + 1
    return len(lines) or start_line


def _collect_javascript_like(text: str, path: str, metadata: dict[str, Any], language: str) -> SyntaxAnalysis:
    lines = text.splitlines()
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    module_name = _module_name(path)
    class_stack: list[tuple[str, int, int]] = []

    def current_prefix() -> str:
        return ".".join([module_name, *[item[0] for item in class_stack]]) if class_stack else module_name

    def add_decl(
        kind: str,
        name: str,
        start_line: int,
        end_line: int,
        extra: dict[str, Any] | None = None,
        exported: bool = False,
        qualified_name: str | None = None,
    ) -> None:
        start_byte, end_byte = _byte_span_from_lines(text, start_line, end_line)
        content = _slice_lines(text, start_line, end_line)
        qualified = qualified_name or (f"{current_prefix()}.{name}" if kind != "module" else current_prefix())
        symbol = _make_symbol(
            metadata,
            path,
            language,
            kind,
            qualified,
            start_line,
            end_line,
            start_byte,
            end_byte,
            content,
            extra={"is_exported": exported, **(extra or {})},
        )
        symbols.append(symbol)
        nodes.append(_make_node(kind, path, language, start_line, end_line, start_byte, end_byte, content, qualified_name=qualified, symbol_kind=kind, node_name=name, extra=extra))

    for line_no, line in enumerate(lines, start=1):
        while class_stack and class_stack[-1][2] < line_no:
            class_stack.pop()
        stripped = line.strip()
        import_match = re.match(r"^(?:export\s+)?import\s+(?P<body>.+?)\s+from\s+['\"](?P<module>[^'\"]+)['\"]", stripped)
        if import_match:
            start_byte, end_byte = _byte_span_from_lines(text, line_no, line_no)
            imports.append(
                _make_import(
                    metadata,
                    path,
                    language,
                    "import",
                    import_match.group("module"),
                    line_no,
                    line_no,
                    start_byte,
                    end_byte,
                    stripped,
                )
            )
            nodes.append(_make_node("import", path, language, line_no, line_no, start_byte, end_byte, stripped, node_name=import_match.group("module")))
        require_match = re.match(r"^(?:const|let|var)\s+.*=\s*require\(['\"](?P<module>[^'\"]+)['\"]\)", stripped)
        if require_match:
            start_byte, end_byte = _byte_span_from_lines(text, line_no, line_no)
            imports.append(_make_import(metadata, path, language, "require", require_match.group("module"), line_no, line_no, start_byte, end_byte, stripped))
            nodes.append(_make_node("import", path, language, line_no, line_no, start_byte, end_byte, stripped, node_name=require_match.group("module")))

        class_match = re.match(r"^(export\s+)?(default\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)(?:\s+extends\s+(?P<base>[A-Za-z_$][\w$.:<>]*))?", stripped)
        if class_match:
            end_line = _match_brace_block(text, line_no)
            class_name = class_match.group("name")
            base = class_match.group("base")
            qualified_name = f"{current_prefix()}.{class_name}"
            add_decl("class", class_name, line_no, end_line, extra={"bases": [base] if base else []}, exported=bool(class_match.group(1)), qualified_name=qualified_name)
            class_stack.append((class_name, line_no, end_line))
            continue

        interface_match = re.match(r"^(export\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)", stripped)
        if interface_match:
            end_line = _match_brace_block(text, line_no)
            add_decl("interface", interface_match.group("name"), line_no, end_line, exported=bool(interface_match.group(1)))
            continue

        enum_match = re.match(r"^(export\s+)?enum\s+(?P<name>[A-Za-z_$][\w$]*)", stripped)
        if enum_match:
            end_line = _match_brace_block(text, line_no)
            add_decl("enum", enum_match.group("name"), line_no, end_line, exported=bool(enum_match.group(1)))
            continue

        fn_match = re.match(r"^(export\s+)?(async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(", stripped)
        if fn_match:
            end_line = _match_brace_block(text, line_no)
            add_decl("function", fn_match.group("name"), line_no, end_line, exported=bool(fn_match.group(1)))
            continue

        method_match = re.match(r"^(?P<indent>\s*)(?:(?:public|private|protected|static|async)\s+)*(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*{", line)
        if method_match and class_stack:
            name = method_match.group("name")
            end_line = _match_brace_block(text, line_no)
            kind = "constructor" if name == "constructor" else "method"
            add_decl(kind, name, line_no, end_line)

    if class_stack:
        parse_errors.append({"kind": "brace_mismatch", "message": "Unbalanced class block"})

    module_end_line = len(lines) or 1
    start_byte, end_byte = _byte_span_from_lines(text, 1, module_end_line)
    content = _slice_lines(text, 1, module_end_line) if text else ""
    symbols.insert(0, _make_symbol(metadata, path, language, "module", module_name, 1, module_end_line, start_byte, end_byte, content))
    nodes.insert(0, _make_node("module", path, language, 1, module_end_line, start_byte, end_byte, content, qualified_name=module_name, symbol_kind="module"))
    if any(symbol["symbol_kind"] != "module" for symbol in symbols):
        unit_kind = "symbol"
        passed = not parse_errors
    else:
        parse_errors.append({"kind": "parse_failure", "message": f"No syntax symbols extracted for {path}"})
        return _fallback_analysis(text, path, metadata, language, parse_errors)
    return SyntaxAnalysis(
        path=path,
        language=language,
        tree_sitter_available=get_parser is not None,
        tree_sitter_version=tree_sitter_package_version(),
        parser_name="tree-sitter" if get_parser else "regex-fallback",
        parser_version=tree_sitter_package_version() or "fallback",
        parse_errors=parse_errors,
        symbols=symbols,
        imports=imports,
        nodes=nodes,
        unit_kind=unit_kind,
        passed=passed,
    )


def _collect_config(text: str, path: str, metadata: dict[str, Any], language: str) -> SyntaxAnalysis:
    try:
        if language == "json":
            parsed = json.loads(text)
        elif language == "yaml":
            parsed = yaml.safe_load(text)
        elif language == "toml":
            parsed = tomllib.loads(text)
        else:
            raise ValueError(language)
    except Exception as exc:
        return _fallback_analysis(text, path, metadata, language, [{"kind": "parse_failure", "message": str(exc)}])

    end_line = len(text.splitlines()) or 1
    start_byte, end_byte = _byte_span_from_lines(text, 1, end_line)
    content = _slice_lines(text, 1, end_line) if text else ""
    module_name = _module_name(path)
    symbol = _make_symbol(
        metadata,
        path,
        language,
        "configuration-object",
        module_name,
        1,
        end_line,
        start_byte,
        end_byte,
        content,
        extra={"parsed_type": type(parsed).__name__},
    )
    node = _make_node("configuration-object", path, language, 1, end_line, start_byte, end_byte, content, qualified_name=module_name, symbol_kind="configuration-object")
    return SyntaxAnalysis(
        path=path,
        language=language,
        tree_sitter_available=get_parser is not None,
        tree_sitter_version=tree_sitter_package_version(),
        parser_name=f"{language}-parser",
        parser_version=tree_sitter_package_version() or "stdlib",
        parse_errors=[],
        symbols=[symbol],
        imports=[],
        nodes=[node],
        unit_kind="configuration-object",
        passed=True,
    )


def _fallback_unit(path: str, metadata: dict[str, Any], text: str, language: str) -> dict[str, Any]:
    lines = text.splitlines()
    end_line = len(lines) or 1
    start_byte, end_byte = _byte_span_from_lines(text, 1, end_line)
    content = _slice_lines(text, 1, end_line) if text else ""
    content_sha256 = sha256_text(content)
    normalized = normalize_path(path)
    return {
        "unit_id": stable_hash(metadata["source_id"], metadata["commit_sha"], normalized, "code_chunk", 1, end_line, content_sha256),
        "source_id": metadata["source_id"],
        "platform": metadata["platform"],
        "repository_url": metadata["repository_url"],
        "namespace": metadata["namespace"],
        "repository_name": metadata["repository_name"],
        "requested_ref": metadata.get("requested_ref"),
        "resolved_branch": metadata.get("resolved_branch"),
        "commit_sha": metadata["commit_sha"],
        "path": path,
        "normalized_path": normalized,
        "unit_type": "code_chunk",
        "heading": None,
        "language": language,
        "start_line": 1,
        "end_line": end_line,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "file_sha256": metadata["file_sha256"],
        "content_sha256": content_sha256,
        "content": content,
        "generator_name": "syntax:fallback",
        "generator_version": "1",
        "schema_version": 1,
        "pipeline_version": metadata["pipeline_version"],
        "source_line_start": 1,
        "source_line_end": end_line,
        "source_byte_start": start_byte,
        "source_byte_end": end_byte,
        "metadata": {"reason": "parse_failure_or_unsupported"},
    }


def _fallback_analysis(text: str, path: str, metadata: dict[str, Any], language: str, parse_errors: list[dict[str, Any]]) -> SyntaxAnalysis:
    return SyntaxAnalysis(
        path=path,
        language=language,
        tree_sitter_available=get_parser is not None,
        tree_sitter_version=tree_sitter_package_version(),
        parser_name="fallback",
        parser_version=tree_sitter_package_version() or "fallback",
        parse_errors=parse_errors,
        fallback_units=[_fallback_unit(path, metadata, text, language)],
        unit_kind="fallback",
        passed=False,
    )


def analyze_file(path: Path, text: str, metadata: dict[str, Any], cfg: dict[str, Any]) -> SyntaxAnalysis:
    language = detect_language(path)
    if language in CONFIG_LANGUAGES:
        return _collect_config(text, str(path), metadata, language)
    if language == "python":
        return _collect_python(text, str(path), metadata, cfg)
    if language in {"javascript", "typescript"}:
        return _collect_javascript_like(text, str(path), metadata, language)
    if language == "shell":
        return _fallback_analysis(text, str(path), metadata, language, [{"kind": "unsupported_language", "message": "shell files use fallback chunking"}])
    return _fallback_analysis(text, str(path), metadata, language, [{"kind": "unsupported_language", "message": f"Unsupported language: {language}"}])
