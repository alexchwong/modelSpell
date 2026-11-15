"""Offline release workflow policy and shell syntax regression checks."""
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_release_builds_without_manual_gate_assets():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert "scripts/build_release.py --smoke --output-dir candidate" in workflow
    assert "provider-gate.json" not in workflow
    assert "--publication-check" not in workflow
    assert "gh release create" in workflow
    assert "gh release edit" in workflow
    assert "sha256sum --check" in workflow
    assert "contents: write" in workflow
    assert "group: modelspell-release" in workflow
    assert "needs: tests" in workflow
    assert "2>/dev/null || true" not in workflow.split("Require version bump")[0]


def test_ci_explicitly_excludes_live_tests():
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    assert '.venv/bin/python -m pytest -m "not live"' in workflow
    assert "workflow_call:" in workflow
    assert '["3.10", "3.13"]' in workflow


def test_release_shell_blocks_parse():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    for block in workflow.split("        run: |\n")[1:]:
        lines = []
        for line in block.splitlines():
            if line and not line.startswith("          "):
                break
            lines.append(line[10:])
        # GitHub expressions are substituted before the runner invokes bash.
        script = re.sub(r"\$\{\{.*?\}\}", "test-value", "\n".join(lines))
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)