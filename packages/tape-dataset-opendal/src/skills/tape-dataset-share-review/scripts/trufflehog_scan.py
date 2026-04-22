#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan a staged tape dataset with TruffleHog and emit a normalized JSON report."
    )
    parser.add_argument("target", help="Directory or file to scan")
    parser.add_argument(
        "--report",
        help="Path to the normalized JSON report. Defaults to <target>.trufflehog.json",
    )
    parser.add_argument(
        "--container-runtime",
        choices=("podman", "docker"),
        help="Force a specific container runtime for fallback mode",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target = Path(args.target).expanduser().resolve()
    if not target.exists():
        print(f"Target not found: {target}", file=sys.stderr)
        return 1

    report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else target.parent / f"{target.name}.trufflehog.json"
    )

    command, scan_path = resolve_command(target, forced_runtime=args.container_runtime)
    raw = run_command(command + trufflehog_args(scan_path))
    findings = parse_findings(raw.stdout)
    report = build_report(target, findings, command)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "target": str(target),
                "report": str(report_path),
                "findings": report["summary"]["findings"],
                "verified": report["summary"]["verified"],
                "unverified": report["summary"]["unverified"],
                "unknown": report["summary"]["unknown"],
                "blocking": report["blocking"],
            },
            sort_keys=True,
        )
    )
    return 0


def resolve_command(target: Path, *, forced_runtime: str | None) -> tuple[list[str], str]:
    binary = shutil.which("trufflehog")
    if binary:
        return ([binary, "filesystem"], str(target))

    runtime = forced_runtime or shutil.which("podman") or shutil.which("docker")
    if not runtime:
        raise SystemExit(
            "Missing required command: trufflehog. Install TruffleHog or provide podman/docker for container fallback."
        )

    parent = target.parent
    mount_target = "/scanroot"
    volume_flag = f"{parent}:{mount_target}:ro"
    if runtime.endswith("podman"):
        volume_flag = f"{parent}:{mount_target}:ro,z"
    scan_path = f"{mount_target}/{target.name}"
    return ([runtime, "run", "--rm", "-v", volume_flag, "docker.io/trufflesecurity/trufflehog:latest", "filesystem"], scan_path)


def trufflehog_args(scan_path: str) -> list[str]:
    return [
        scan_path,
        "-j",
        "--results=verified,unknown,unverified",
        "--no-color",
        "--no-update",
    ]


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip() or "trufflehog scan failed")
    return result


def parse_findings(stdout: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        finding = parse_finding(payload)
        if finding is None:
            continue
        key = json.dumps(
            [
                finding["detector"],
                finding.get("decoder"),
                finding["status"],
                finding.get("file"),
                finding.get("line"),
                finding.get("raw_sha256"),
            ],
            sort_keys=True,
        )
        if key not in seen:
            seen.add(key)
            findings.append(finding)
    findings.sort(
        key=lambda item: (
            str(item.get("file", "")),
            item.get("line") if isinstance(item.get("line"), int) else 10**9,
            str(item["detector"]),
        )
    )
    return findings


def parse_finding(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    detector = payload.get("DetectorName")
    if not isinstance(detector, str) or not detector:
        return None
    raw = payload.get("Raw")
    filesystem = read_filesystem_metadata(payload)
    status = parse_status(payload)
    return {
        "detector": detector,
        "decoder": payload.get("DecoderName") if isinstance(payload.get("DecoderName"), str) else None,
        "status": status,
        "file": filesystem.get("file"),
        "line": filesystem.get("line"),
        "verification_from_cache": payload.get("VerificationFromCache") is True,
        "raw_sha256": sha256_text(raw) if isinstance(raw, str) and raw else None,
        "masked": mask_secret(raw) if isinstance(raw, str) and raw else "[REDACTED]",
    }


def parse_status(payload: dict[str, Any]) -> str:
    if payload.get("Verified") is True:
        return "verified"
    extra_data = payload.get("ExtraData")
    if isinstance(extra_data, dict):
        for key in ("verification_error", "verificationError", "error"):
            value = extra_data.get(key)
            if isinstance(value, str) and value.strip():
                return "unknown"
    return "unverified"


def read_filesystem_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    source_metadata = payload.get("SourceMetadata")
    if not isinstance(source_metadata, dict):
        return {}
    data = source_metadata.get("Data")
    if not isinstance(data, dict):
        return {}
    filesystem = data.get("Filesystem")
    if not isinstance(filesystem, dict):
        return {}
    result: dict[str, Any] = {}
    if isinstance(filesystem.get("file"), str):
        result["file"] = filesystem["file"]
    if isinstance(filesystem.get("line"), int):
        result["line"] = filesystem["line"]
    return result


def build_report(target: Path, findings: list[dict[str, Any]], command: list[str]) -> dict[str, Any]:
    counter = Counter(finding["status"] for finding in findings)
    detectors = Counter(finding["detector"] for finding in findings)
    return {
        "target": str(target),
        "command": command,
        "findings": findings,
        "summary": {
            "findings": len(findings),
            "verified": counter.get("verified", 0),
            "unverified": counter.get("unverified", 0),
            "unknown": counter.get("unknown", 0),
            "top_detectors": [detector for detector, _ in detectors.most_common(8)],
        },
        "blocking": len(findings) > 0,
    }


def sha256_text(value: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def mask_secret(raw: str) -> str:
    if len(raw) <= 8:
        return "***"
    prefix_length = min(8, max(4, len(raw) // 4))
    suffix_length = min(4, len(raw) - prefix_length)
    return f"{raw[:prefix_length]}***{raw[-suffix_length:]}"


if __name__ == "__main__":
    raise SystemExit(main())
