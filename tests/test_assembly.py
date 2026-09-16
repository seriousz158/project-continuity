"""Assembly integrity of the shipped scripts.

The 0.3.2 candidate carried two top-level ``read_bytes`` definitions in
``storage.py``.  Python keeps the last one, so the window-aware implementation
was dead code and every read of a file that was mid-publication failed.  These
checks are the permanent gate for that class of defect: a duplicated top-level
name, a build residue left inside ``scripts/`` or a signature that lost the
publication-window parameter must fail the suite.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location("assembly_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def top_level_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(target.id for target in node.targets
                         if isinstance(target, ast.Name))
    return names


class AssemblyIntegrityTests(unittest.TestCase):
    def test_every_script_parses(self):
        scripts = sorted(SCRIPTS.glob("*.py"))
        self.assertTrue(scripts, "no scripts were found to inspect")
        for path in scripts:
            with self.subTest(script=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_no_duplicate_top_level_definitions(self):
        """A later definition silently shadows an earlier one."""
        for path in sorted(SCRIPTS.glob("*.py")):
            names = top_level_names(path)
            duplicates = sorted({name for name in names if names.count(name) > 1})
            with self.subTest(script=path.name):
                self.assertEqual(duplicates, [],
                                 "duplicate top-level definitions in " + path.name)

    def test_no_assembly_residue_inside_scripts(self):
        residue = sorted(path.name for path in SCRIPTS.iterdir()
                         if path.is_file() and path.name.startswith("_")
                         and path.suffix == ".txt")
        self.assertEqual(residue, [], "assembly residue would ship with the skill")

    def test_storage_read_path_is_window_aware(self):
        storage = load("storage")
        signature = inspect.signature(storage.read_bytes)
        self.assertIn("temporary_pattern", signature.parameters)
        source = (SCRIPTS / "storage.py").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"^def read_bytes\(", source, re.M)), 1,
                         "read_bytes must be defined exactly once at module level")


if __name__ == "__main__":
    unittest.main()
