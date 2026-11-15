#!/usr/bin/env python3
"""Verify modelSpell's OpenRouter refactor against an isolated candidate wheel."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import uuid
import venv
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ("modelspell", "pyproject.toml", "README.md", "LICENSE")
HARNESS = ("scripts", "tests", "ui")
KEY = "OPENROUTER_API_KEY"
VT = "Automatic Balanced ZDR tool round trip / VT"
BUILD_KEYS = ("commit", "version", "wheel_sha256")


class VerificationError(Exception):
    pass


def clean_environment():
    return {k: v for k, v in os.environ.items() if k not in (KEY, "PYTHONPATH", "PYTHONHOME")}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, env=clean_environment()).decode().strip()


def clean(paths):
    return not git("status", "--porcelain=v1", "--untracked-files=all", "--", *paths)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def python_in(path):
    return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(cmd, *, cwd=ROOT, **kw):
    return subprocess.run(cmd, cwd=cwd, env=clean_environment(), **kw)


def fresh_report(args, commit):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    version_match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$',
                              git("show", f"{commit}:modelspell/_version.py"), re.M)
    if not version_match:
        raise VerificationError("committed modelSpell version unavailable")
    return {"run_id": f"{stamp}-{uuid.uuid4().hex[:12]}",
            "build": {"commit": commit, "version": version_match.group(1), "wheel_sha256": None},
            "provenance": {"payload_clean_start": clean(PAYLOAD), "harness_clean_start": clean(HARNESS)},
            "cases": [], "incidents": [], "complete_vt": False,
            "overrides": {"model": args.release_model, "max_input_price": args.release_max_input_price,
                          "max_output_price": args.release_max_output_price}}


def valid_identity(build):
    return (isinstance(build, dict)
            and all(isinstance(build.get(k), str) for k in BUILD_KEYS)
            and re.fullmatch(r"[a-f0-9]{40}", build["commit"])
            and re.fullmatch(r"\d+\.\d+\.\d+", build["version"])
            and re.fullmatch(r"[a-f0-9]{64}", build["wheel_sha256"]))


def eligible(report):
    p = report["provenance"]
    b = report["build"]
    return bool(valid_identity(b) and all(p.get(k) is True for k in (
        "payload_clean_start", "payload_clean_end", "harness_clean_start", "harness_clean_end",
        "committed_harness", "installed_wheel", "wheel_smoke", "wheel_verified_end"))
        and p.get("head_end") == b["commit"] == p.get("builder_commit"))


def outcomes(report, verdicts=None):
    verdicts = verdicts or {}
    for case in report["cases"]:
        case["state"], case["reason"] = case["observed_state"], case["observed_reason"]
        relevant = [verdicts.get(i["id"]) for i in report["incidents"] if i["case"] == case["name"]]
        if "modelspell" in relevant:
            case.update(state="FAIL", reason="recorded modelSpell defect")
        elif None in relevant and case["state"] != "FAIL":
            case.update(state="BLOCKED", reason="response rejected — triage required")
    named = next((case for case in report["cases"] if case["name"] == VT), None)
    failed = any(c["state"] == "FAIL" for c in report["cases"])
    gate = ("fail" if named and named["state"] == "FAIL" else
            "pass" if named and named["state"] == "PASS" and report["complete_vt"] and eligible(report)
            else "pending")
    if gate == "pending" and named and named["state"] == "PASS":
        named.update(state="BLOCKED", reason="ineligible candidate provenance or incomplete VT")
    code = 1 if failed else 3 if gate != "pass" else 2 if any(
        case["state"] == "BLOCKED" for case in report["cases"]) else 0
    report.update(release_gate=gate, exit_code=code)
    return code


def serialized(report):
    value = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    key = os.environ.get(KEY, "").strip()
    if key and (key in value or json.dumps(key, ensure_ascii=False)[1:-1] in value):
        raise VerificationError("API credential found in report")
    return value


def save(path, report):
    value = serialized(report)
    with path.open("x", encoding="utf-8") as file:
        file.write(value)


def emit(report, path):
    for case in report["cases"]:
        label = case["state"]
        reason = f" ({case['reason']})" if case["reason"] else ""
        costs = []
        for row in case.get("observations", []):
            costs.append(row.get("cost"))
            costs.extend(a.get("cost") for a in row.get("evidence", {}).get("attempts", []))
        known = [v for v in costs if isinstance(v, (int, float))]
        cost = f", reported charges USD {sum(known):.8g}" if known else ""
        print(f"{label} {case['name']}{reason} — {case.get('posts', 0)} POSTs, "
              f"{case.get('reserved', 0)} reserved{cost}")
    print(f"report {path}")
    if report.get("wheel"):
        print(f"wheel {report['wheel']}")
        print(f"sha256 {report['build']['wheel_sha256']}")
    print(f"verification exit {report['exit_code']}")
    print(f"release_gate {report['release_gate']}")


def valid_install(wheel, build):
    import modelspell

    distribution = importlib.metadata.distribution("modelSpell")
    installed = Path(modelspell.__file__).resolve()
    expected = distribution.locate_file("modelspell/__init__.py").resolve()
    prefix = Path(sys.prefix).resolve()
    if (not valid_identity(build) or sha(wheel) != build["wheel_sha256"] or installed != expected
            or not installed.is_relative_to(prefix) or sys.prefix == sys.base_prefix
            or distribution.version != build["version"] or modelspell.__version__ != build["version"]):
        return False
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(f"modelspell-{build['version']}.dist-info/METADATA")
        return (f"\nVersion: {build['version']}\n".encode() in metadata
                and all(distribution.locate_file(name).read_bytes() == archive.read(name)
                        for name in archive.namelist() if name.startswith("modelspell/") and not name.endswith("/")))


def live_worker(input_path):
    # -I keeps the checkout off sys.path. Add only the committed scripts directory
    # after importing and validating the package against the installed wheel.
    report = json.loads(input_path.read_text())
    wheel = Path(report["wheel"])
    report["provenance"]["installed_wheel"] = valid_install(wheel, report["build"])
    if report["provenance"]["installed_wheel"]:
        sys.path.append(str(Path(__file__).resolve().parent))
        from provider_live import worker
        worker(report)
    else:
        add_case(report, VT, "BLOCKED", "wheel or installed import identity could not be established")
    sys.stdout.write(serialized(report))


def add_case(report, name, state, reason=None):
    report["cases"].append({"name": name, "observed_state": state, "observed_reason": reason,
                            "state": state, "reason": reason, "posts": 0, "reserved": 0,
                            "observations": []})


def candidate(report, folder):
    commit = report["build"]["commit"]
    with tempfile.TemporaryDirectory(prefix="modelspell-provider-") as scratch_dir:
        scratch = Path(scratch_dir)
        archive = scratch / "source.tar"
        git("archive", "--format=tar", f"--output={archive}", commit)
        with tarfile.open(archive) as tar:
            tar.extractall(scratch / "source", filter="data")
        scripts = scratch / "source" / "scripts"
        runner = scripts / Path(__file__).name
        if not runner.exists():
            add_case(report, VT, "BLOCKED", "committed verification harness is unavailable")
            return report
        built = run([sys.executable, str(ROOT / "scripts" / "build_release.py"), "--commit", commit,
                     "--smoke", "--output-dir", str(folder)], capture_output=True, text=True)
        if built.returncode != 0:
            add_case(report, "Installed-wheel smoke", "FAIL", "candidate build or installed-wheel smoke failed")
            return report
        wheels = list(folder.glob("*.whl"))
        if len(wheels) != 1:
            raise VerificationError("candidate builder did not produce exactly one wheel")
        wheel = wheels[0]
        digest = sha(wheel)
        report["wheel"] = str(wheel)
        report["build"]["wheel_sha256"] = digest
        report["provenance"].update(
            builder_commit=commit if f"source commit {commit}" in built.stdout.splitlines() else None,
            wheel_smoke=f"sha256 {digest}" in built.stdout.splitlines(),
            committed_harness=True,
        )
        add_case(report, "Installed-wheel smoke", "PASS" if report["provenance"]["wheel_smoke"] else "FAIL",
                 None if report["provenance"]["wheel_smoke"] else "builder and wheel digest differ")
        env_dir = scratch / "wheel-venv"
        venv.EnvBuilder(with_pip=True).create(env_dir)
        python = python_in(env_dir)
        run([str(python), "-I", "-m", "pip", "install", "-q", "--no-index", str(wheel)],
            cwd=scratch, check=True)
        input_path = scratch / "worker-input.json"
        save(input_path, report)
        env = clean_environment()
        if KEY in os.environ:
            env[KEY] = os.environ[KEY]
        result = subprocess.run([str(python), "-I", str(runner), "--worker", str(input_path)],
                                cwd=scratch, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise VerificationError("installed-wheel verifier stopped before producing safe results")
        report = json.loads(result.stdout)
        report["provenance"]["wheel_verified_end"] = sha(wheel) == digest
        return report


def verification(args):
    commit = git("rev-parse", "HEAD")
    report = fresh_report(args, commit)
    folder = ROOT / "reports" / "provider-refactor" / report["run_id"]
    folder.mkdir(parents=True)
    offline = run([sys.executable, "-m", "pytest", "-m", "not live"])
    add_case(report, "Offline suite", "PASS" if offline.returncode == 0 else "FAIL",
             None if offline.returncode == 0 else "offline pytest failed")
    if offline.returncode == 0:
        try:
            report = candidate(report, folder)
        except (VerificationError, subprocess.SubprocessError, OSError, ValueError) as exc:
            add_case(report, "Candidate verifier", "FAIL", f"candidate verification raised {type(exc).__name__}")
    report["provenance"].update(payload_clean_end=clean(PAYLOAD), harness_clean_end=clean(HARNESS),
                                head_end=git("rev-parse", "HEAD"))
    code = outcomes(report)
    path = folder / "report.json"
    save(path, report)
    emit(report, path)
    return code


def triage(report_path, incident_id, verdict, reason):
    if verdict not in ("upstream", "modelspell") or not reason.strip() or len(reason) > 500:
        raise VerificationError("provide upstream or modelspell and an evidence-based reason of up to 500 characters")
    report = json.loads(report_path.read_text())
    if not valid_identity(report["build"]):
        raise VerificationError("the original report has no complete tested build identity")
    prefix = "/".join([report["run_id"], *(report["build"][k] for k in BUILD_KEYS)]) + "/"
    matches = [i for i in report["incidents"] if i["id"] == incident_id]
    if (len(matches) != 1 or not incident_id.startswith(prefix)
            or matches[0]["run_id"] != report["run_id"] or matches[0]["build"] != report["build"]):
        raise VerificationError("incident does not belong to this run and build")
    original_digest = sha(report_path)
    verdicts = {}
    for record_path in sorted(report_path.parent.glob(f"triage-{original_digest}-*.json")):
        record = json.loads(record_path.read_text())
        if (record["run_id"] != report["run_id"] or record["build"] != report["build"]
                or record["original_sha256"] != original_digest
                or not record["incident_id"].startswith(prefix)
                or not any(i["id"] == record["incident_id"] and i["build"] == report["build"]
                           and i["run_id"] == report["run_id"] for i in report["incidents"])
                or record["verdict"] not in ("upstream", "modelspell")):
            raise VerificationError("triage record does not match the original run and build")
        verdicts[record["incident_id"]] = record["verdict"]
    if incident_id in verdicts:
        raise VerificationError("incident already has a recorded verdict")
    verdicts[incident_id] = verdict
    code = outcomes(report, verdicts)
    record = {"run_id": report["run_id"], "build": report["build"], "original_sha256": original_digest,
              "incident_id": incident_id, "verdict": verdict, "reason": reason, "updated_report": report,
              "verdicts": verdicts}
    path = report_path.parent / f"triage-{original_digest}-{incident_id.rsplit('/', 1)[1]}.json"
    save(path, record)
    emit(report, path)
    return code


def completed_configuration(report):
    """Check the recorded gate against the actual selection and both recorded VT sequences."""
    case = next((c for c in report["cases"] if c["name"] == VT), {})
    rows = case.get("observations", ())
    for row in rows:
        if row.get("phase") != "VT":
            continue
        if row.get("sequences") != [{"stream": False, "reason": None}, {"stream": True, "reason": None}]:
            continue
        policy = row.get("policy", {})
        pin = row.get("pin")
        if (not pin or policy.get("zdr") is not True or policy.get("order") != [pin]
                or policy.get("allow_fallbacks") is not False or policy.get("require_parameters") is not True):
            continue
        for choice in rows:
            if (choice.get("phase") != "selection" or choice.get("outcome") != "accepted"
                    or choice.get("model") != row.get("model") or choice.get("pin") != pin
                    or choice.get("policy") != policy):
                continue
            ev = choice["evidence"]
            if (ev.get("preset") == "balanced" and ev.get("mode") == "automatic" and ev.get("threshold") == 50
                    and any(r.get("slug") == pin and "excluded" not in r and (r.get("p50_tps") or 0) > 50
                            for r in ev.get("discovery", ()))
                    and any(a["slug"] == pin and a["tools"] is True and a["outcome"] == "accepted"
                            for a in ev.get("attempts", ()))):
                return True
    return False


def publication(report_path, wheel, version):
    data = json.loads(report_path.read_text())
    verdicts = data.get("verdicts", {})
    report = data.get("updated_report", data)
    if report is not data:
        if (data.get("run_id") != report.get("run_id") or data.get("build") != report.get("build")
                or not re.fullmatch(r"[a-f0-9]{64}", data.get("original_sha256", ""))
                or verdicts.get(data.get("incident_id")) != data.get("verdict")):
            raise VerificationError("staged triage record has a mismatched run or build")
    elif verdicts:
        raise VerificationError("stage a recorded triage verdict with the updated report")
    for incident_id, verdict in verdicts.items():
        if verdict not in ("upstream", "modelspell") or not any(
            i["id"] == incident_id and i["build"] == report["build"] and i["run_id"] == report["run_id"]
            for i in report["incidents"]
        ):
            raise VerificationError("staged triage verdict has a mismatched incident identity")
    if (outcomes(report, verdicts) not in (0, 2) or report["release_gate"] != "pass"
            or not completed_configuration(report) or not eligible(report) or version != report["build"]["version"]
            or not any(c["name"] == "Offline suite" and c["state"] == "PASS" for c in report["cases"])
            or not any(c["name"] == "Installed-wheel smoke" and c["state"] == "PASS" for c in report["cases"])):
        raise VerificationError("a qualifying named live gate report is required")
    commit = report["build"]["commit"]
    git("rev-parse", "--verify", f"{commit}^{{commit}}")
    if git("diff", "--name-only", commit, "HEAD", "--", *PAYLOAD):
        raise VerificationError("main payload differs from the verified candidate")
    if wheel.name != f"modelspell-{version}-py3-none-any.whl" or sha(wheel) != report["build"]["wheel_sha256"]:
        raise VerificationError("wheel digest or filename differs from the verified candidate")
    print(f"commit={commit}")
    print(f"sha256={report['build']['wheel_sha256']}")
    return 0


def price(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("price caps must be finite nonnegative USD per million tokens")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-model")
    parser.add_argument("--release-max-input-price", type=price, default=0.50)
    parser.add_argument("--release-max-output-price", type=price, default=1.50)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--triage", nargs=4, metavar=("REPORT", "INCIDENT", "VERDICT", "REASON"))
    group.add_argument("--publication-check", nargs=3, metavar=("REPORT", "WHEEL", "VERSION"))
    group.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            live_worker(args.worker)
            return 0
        if args.triage:
            path, incident, verdict, reason = args.triage
            return triage(Path(path), incident, verdict, reason)
        if args.publication_check:
            path, wheel, version = args.publication_check
            return publication(Path(path), Path(wheel), version)
        python = python_in(ROOT / ".venv")
        if Path(sys.prefix).absolute() != python.parent.parent:
            if not python.exists():
                venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
            os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])
        probe = run([sys.executable, "-c", "import pytest, modelspell"], capture_output=True)
        if probe.returncode:
            run([sys.executable, "-m", "pip", "install", "-e", ".[dev]"], check=True)
        return verification(args)
    except VerificationError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        print("FAIL verification could not read or validate the candidate or report", file=sys.stderr)
    if not args.worker:
        print("verification exit 1")
        print("release_gate fail" if not args.triage else "release_gate pending")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
