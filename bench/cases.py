"""Case catalog loader and isolated patch applier for Cases 01..10."""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "cases"


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    title: str
    body: str
    patch_path: Path
    patch_text: str
    evidence_path: Path | None
    evidence_md: str | None
    evidence_json: dict[str, Any] | None
    is_held_out: bool
    category: str  # "code_defect", "env_dependent", "clean"
    expected_code_only: str  # "TP", "ENV-ASK", "TN"
    expected_with_snapshot: str | None  # "ENV-TP" or None


CASE_METADATA: dict[str, tuple[bool, str, str, str | None]] = {
    "01": (False, "code_defect", "TP", None),
    "02": (False, "code_defect", "TP", None),
    "03": (False, "code_defect", "TP", None),
    "04": (False, "env_dependent", "ENV-ASK", "ENV-TP"),
    "05": (False, "env_dependent", "ENV-ASK", "ENV-TP"),
    "06": (False, "env_dependent", "ENV-ASK", "ENV-TP"),
    "07": (False, "clean", "TN", None),
    "08": (False, "clean", "TN", None),
    "09": (True, "code_defect", "TP", None),
    "10": (True, "clean", "TN", None),
}


def _extract_json_block(md_text: str) -> dict[str, Any] | None:
    match = re.search(r"```json\s*(\{.*?\})\s*```", md_text, flags=re.DOTALL)
    if not match:
        return None
    return json.loads(match.group(1))


def load_cases(include_held_out: bool = True) -> dict[str, CaseSpec]:
    catalog: dict[str, CaseSpec] = {}
    for case_dir in sorted(p for p in CASES_DIR.iterdir() if p.is_dir() and p.name.isdigit()):
        cid = case_dir.name
        is_held_out, category, exp_code, exp_snap = CASE_METADATA[cid]
        if is_held_out and not include_held_out:
            continue
        title = (case_dir / "title.txt").read_text(encoding="utf-8").strip()
        body = (case_dir / "body.md").read_text(encoding="utf-8").strip()
        patch_path = case_dir / "change.patch"
        patch_text = patch_path.read_text(encoding="utf-8")
        ev_path = case_dir / "evidence.md"
        ev_md = ev_path.read_text(encoding="utf-8").strip() if ev_path.exists() else None
        ev_json = _extract_json_block(ev_md) if ev_md else None
        catalog[cid] = CaseSpec(
            case_id=cid,
            title=title,
            body=body,
            patch_path=patch_path,
            patch_text=patch_text,
            evidence_path=ev_path if ev_path.exists() else None,
            evidence_md=ev_md,
            evidence_json=ev_json,
            is_held_out=is_held_out,
            category=category,
            expected_code_only=exp_code,
            expected_with_snapshot=exp_snap,
        )
    return catalog


def apply_unified_patch(base_dir: Path, patch_text: str) -> None:
    """Apply a unified diff (supporting modified files and new file creation) inside base_dir."""
    lines = patch_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("diff --git "):
            i += 1
            continue
        i += 1
        old_path = None
        new_path = None
        while i < len(lines) and not lines[i].startswith("@@ "):
            if lines[i].startswith("--- "):
                raw_old = lines[i][4:].strip()
                old_path = None if raw_old == "/dev/null" else raw_old.removeprefix("a/")
            elif lines[i].startswith("+++ "):
                raw_new = lines[i][4:].strip()
                new_path = None if raw_new == "/dev/null" else raw_new.removeprefix("b/")
            i += 1
        target_rel = new_path or old_path
        if not target_rel:
            raise ValueError(f"Malformed patch header near line {i}")
        base_resolved = base_dir.resolve()
        target_file = (base_dir / target_rel).resolve()
        if not target_file.is_relative_to(base_resolved):
            raise ValueError(f"Refusing to apply patch path outside base_dir: {target_rel}")
        orig_lines = (
            target_file.read_text(encoding="utf-8").splitlines()
            if (old_path is not None and target_file.exists())
            else []
        )
        out_lines: list[str] = []
        orig_idx = 0
        while i < len(lines) and not lines[i].startswith("diff --git "):
            header = lines[i]
            if not header.startswith("@@ "):
                i += 1
                continue
            m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
            if not m:
                raise ValueError(f"Invalid hunk header: {header}")
            old_start = int(m.group(1))
            target_idx = max(0, old_start - 1)
            while orig_idx < target_idx and orig_idx < len(orig_lines):
                out_lines.append(orig_lines[orig_idx])
                orig_idx += 1
            i += 1
            while i < len(lines) and not lines[i].startswith("@@ ") and not lines[i].startswith("diff --git "):
                hline = lines[i]
                if hline.startswith("\\ No newline"):
                    i += 1
                    continue
                prefix = hline[0] if hline else " "
                content = hline[1:] if hline else ""
                if prefix == " ":
                    if orig_idx >= len(orig_lines) or orig_lines[orig_idx] != content:
                        got = orig_lines[orig_idx] if orig_idx < len(orig_lines) else "<EOF>"
                        raise ValueError(f"Patch context mismatch in {target_rel}: expected {content!r}, got {got!r}")
                    out_lines.append(content)
                    orig_idx += 1
                elif prefix == "-":
                    if orig_idx >= len(orig_lines) or orig_lines[orig_idx] != content:
                        got = orig_lines[orig_idx] if orig_idx < len(orig_lines) else "<EOF>"
                        raise ValueError(f"Patch delete mismatch in {target_rel}: expected {content!r}, got {got!r}")
                    orig_idx += 1
                elif prefix == "+":
                    out_lines.append(content)
                else:
                    raise ValueError(f"Unexpected diff prefix {prefix!r} in {target_rel}")
                i += 1
        while orig_idx < len(orig_lines):
            out_lines.append(orig_lines[orig_idx])
            orig_idx += 1
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def materialize_app(dest_dir: Path, case_id: str | None = None) -> Path:
    """Copy clean pilot/base application files into dest_dir (preferring git baseline snapshot) and optionally apply case_id's patch."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for rel in ["reports.py", "console.py", "migrations/001_initial.sql"]:
        dst = dest_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        loaded_from_git = False
        for rev in ("9b5ac64ab0d3ba414c5edec1e64fcdf8b3b19a1a", "79d03777827b88aa23ee9309ebefab5f9766cd09"):
            proc = subprocess.run(
                ["git", "-C", str(ROOT), "show", f"{rev}:{rel}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                dst.write_text(proc.stdout, encoding="utf-8")
                loaded_from_git = True
                break
        if not loaded_from_git:
            shutil.copy2(ROOT / rel, dst)
    if case_id is not None:
        spec = load_cases(include_held_out=True)[case_id]
        apply_unified_patch(dest_dir, spec.patch_text)
    return dest_dir


def load_patched_reports_module(app_dir: Path, module_name: str = "reports_patched") -> ModuleType:
    """Dynamically load reports.py from app_dir so its __file__ points to app_dir/migrations."""
    reports_path = app_dir / "reports.py"
    spec = importlib.util.spec_from_file_location(module_name, reports_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {reports_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
