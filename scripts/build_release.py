#!/usr/bin/env python3
"""Build and verify the modelSpell release wheel from committed files.

The wheel is the only release artifact. It is built from `git archive HEAD`, so uncommitted
edits never leak in, and verified to contain exactly the `modelspell/` package plus metadata and
LICENSE. Usage (from the repository root):

    python scripts/build_release.py [--output-dir dist] [--smoke] [--no-isolation]
    python scripts/build_release.py --changed-since v0.1.0   # exit 1 if shipped files changed
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "modelspell"
DIST_NAME = "modelSpell"
# Files whose change requires a version bump: everything that ends up in the wheel or its metadata.
PAYLOAD = ["modelspell", "pyproject.toml", "README.md", "LICENSE"]
VERSION_RE = re.compile(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', re.M)


def fail(message: str) -> None:
    raise SystemExit(f"build_release: {message}")


def git(*args: str, **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, **kw)
    except FileNotFoundError:
        fail("git is required")
    except subprocess.CalledProcessError as exc:
        fail(f"git {' '.join(args)} failed: {exc.stderr.decode(errors='replace').strip()}")


def read_version(tree: Path) -> str:
    match = VERSION_RE.search((tree / PACKAGE / "_version.py").read_text(encoding="utf-8"))
    if not match:
        fail(f"{PACKAGE}/_version.py must contain exactly: __version__ = \"X.Y.Z\"")
    return match.group(1)


def check_news(tree: Path, version: str) -> None:
    news = (tree / "NEWS.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(version)}\s*$", news, re.M):
        fail(f"NEWS.md has no '## {version}' heading")


def export_head(dest: Path) -> None:
    archive = dest / "head.tar"
    git("archive", "--format=tar", f"--output={archive}", "HEAD")
    with tarfile.open(archive) as tar:
        tar.extractall(dest / "src", filter="data")


def build_wheel(src: Path, out: Path, isolation: bool) -> Path:
    cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(out)]
    cmd += [] if isolation else ["--no-build-isolation"]
    cmd.append(str(src))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"wheel build failed:\n{result.stdout}{result.stderr}")
    wheels = list(out.glob("*.whl"))
    if len(wheels) != 1:
        fail(f"expected one wheel, found {wheels}")
    return wheels[0]


def verify_wheel(wheel: Path, src: Path, version: str) -> None:
    dist_info = f"{PACKAGE}-{version}.dist-info"
    expected = {p.relative_to(src).as_posix() for p in (src / PACKAGE).rglob("*")
                if p.is_file() and "__pycache__" not in p.parts}
    expected |= {f"{dist_info}/{n}" for n in ("METADATA", "WHEEL", "RECORD", "licenses/LICENSE")}
    if wheel.name != f"{PACKAGE}-{version}-py3-none-any.whl":
        fail(f"unexpected wheel name {wheel.name}")
    with zipfile.ZipFile(wheel) as zf:
        if zf.testzip() is not None:
            fail("wheel contains a corrupt member")
        actual = {n for n in zf.namelist() if not n.endswith("/")}
        metadata = zf.read(f"{dist_info}/METADATA").decode()
    missing, extra = sorted(expected - actual), sorted(actual - expected)
    if missing or extra:
        fail(f"wheel contents differ from {PACKAGE}/.\nMissing: {missing}\nExtra: {extra}")
    for field in (f"Name: {DIST_NAME}", f"Version: {version}", "License-Expression: MIT"):
        if f"\n{field}\n" not in f"\n{metadata}":
            fail(f"wheel METADATA lacks '{field}'")


def smoke(wheel: Path, src: Path, scratch: Path) -> None:
    env_dir = scratch / "venv"
    venv.EnvBuilder(with_pip=True).create(env_dir)
    py = env_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run([py, "-m", "pip", "install", "-q", "--no-index", str(wheel)], check=True)
    out = subprocess.run([py, "-I", str(src / "scripts" / "smoke_installed.py")], cwd=scratch,
                         check=True, capture_output=True, text=True).stdout
    if str(env_dir) not in out:
        fail(f"smoke test did not import the installed wheel: {out}")


def changed_since(tag: str) -> bool:
    git("rev-parse", "--verify", f"{tag}^{{commit}}")
    diff = subprocess.run(["git", "diff", "--quiet", f"{tag}..HEAD", "--", *PAYLOAD], cwd=ROOT)
    return diff.returncode != 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--smoke", action="store_true", help="install into a fresh venv and run the smoke test")
    parser.add_argument("--no-isolation", action="store_true",
                        help="build with the current environment's hatchling (offline builds)")
    parser.add_argument("--changed-since", metavar="TAG",
                        help="only report whether shipped files changed since TAG (exit 1 if they did)")
    args = parser.parse_args()

    if args.changed_since:
        changed = changed_since(args.changed_since)
        print(f"shipped files {'changed' if changed else 'unchanged'} since {args.changed_since}")
        raise SystemExit(1 if changed else 0)

    if git("status", "--porcelain").stdout.strip():
        print("build_release: note: uncommitted changes are NOT included (building from HEAD)")
    with tempfile.TemporaryDirectory(prefix="modelspell-release-") as tmp:
        scratch = Path(tmp)
        export_head(scratch)
        src = scratch / "src"
        version = read_version(src)
        check_news(src, version)
        wheel = build_wheel(src, scratch / "dist", isolation=not args.no_isolation)
        verify_wheel(wheel, src, version)
        if args.smoke:
            smoke(wheel, src, scratch)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        target = args.output_dir / wheel.name
        # copy rather than replace: the temp dir may be on another filesystem
        shutil.copy2(wheel, target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"built {target}\nsha256 {digest}")


if __name__ == "__main__":
    main()
