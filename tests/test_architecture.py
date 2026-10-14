"""The dependency direction is a tested invariant, not a diagram.

The platform has a data side (legacy readers, lakehouse, quality,
reconciliation) and a semantic side (ontology, policy, agents, llm). The
contract: the data side never knows the semantic side exists, and the
semantic side touches the data side only through the lakehouse facade,
never through raw sources or pipeline internals. Orchestrators (cli, api,
packs, export, evals, mcp_server) may import both; that is their job.

This walks the import graph with ast, so a violating import fails the
suite the moment it is written.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1] / "ironwright"

DATA_MODULES = {"legacy", "lake", "quality", "reconcile"}
SEMANTIC_MODULES = {"ontology", "agents", "llm", "policy"}
# The one sanctioned crossing: the semantic side may import the lakehouse
# facade (ironwright.lake), because the lake is the shared substrate.
SEMANTIC_ALLOWED_DATA_IMPORTS = {"lake"}


def _ironwright_imports(path: pathlib.Path) -> set[str]:
    """Top-level ironwright module names imported by the file."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # Relative import: resolve the first segment against the
                # package root. ``from .lake import X`` inside ironwright/
                # or ``from ..lake import X`` inside ironwright/ontology/.
                if node.module:
                    found.add(node.module.split(".")[0])
            elif node.module and node.module.startswith("ironwright"):
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("ironwright."):
                    found.add(alias.name.split(".")[1])
    return found


def _module_files(names: set[str]) -> list[pathlib.Path]:
    files = []
    for name in names:
        mod = ROOT / f"{name}.py"
        pkg = ROOT / name
        if mod.exists():
            files.append(mod)
        elif pkg.is_dir():
            files.extend(sorted(pkg.rglob("*.py")))
        else:
            raise AssertionError(f"layer module {name} does not exist; update the layer map")
    return files


def test_data_side_never_imports_the_semantic_side():
    for f in _module_files(DATA_MODULES):
        bad = _ironwright_imports(f) & SEMANTIC_MODULES
        assert not bad, f"{f.relative_to(ROOT.parent)} imports semantic modules {sorted(bad)}"


def test_semantic_side_touches_data_only_through_the_lakehouse():
    banned = DATA_MODULES - SEMANTIC_ALLOWED_DATA_IMPORTS
    for f in _module_files(SEMANTIC_MODULES):
        bad = _ironwright_imports(f) & banned
        assert not bad, f"{f.relative_to(ROOT.parent)} imports data internals {sorted(bad)}"
