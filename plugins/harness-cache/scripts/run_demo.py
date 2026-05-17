#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


def repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists() and (parent / "harness_cache").is_dir():
            return parent
    raise RuntimeError("Could not locate Harness Cache repository root.")


ROOT = repo_root()
sys.path.insert(0, str(ROOT))

from examples.demo_token_savings import TASKS, build_cache, run_demo
from harness_cache import CachedAgentWorkflow, compare_reports


TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "non_cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


@dataclass(frozen=True)
class CodexRun:
    label: str
    prompt: str
    command: list[str]
    token_usage: dict[str, int | str]
    stdout: str
    stderr: str


def inspect_cache() -> dict:
    cache = build_cache()
    workflow = CachedAgentWorkflow(cache)
    reports = []
    for task in TASKS:
        session = workflow.start_task(task["agent_id"], task["task"])
        ranges = session.find_evidence(limit=5, required_tags=task["required_tags"])
        if ranges:
            pointer_id = ranges[0].pointer.pointer_id
            session.use(pointer_id)
            session.cite(pointer_id, correct=True)
            session.test_passed(pointer_id)
            reports.append(session.complete(success=True, supported_key_claims=1, total_key_claims=1))
        else:
            reports.append(session.complete(success=False))

    comparison = compare_reports(reports)
    return {
        "stores": {
            "l1_agents": sorted(cache.l1),
            "l2": sorted(cache.l2),
            "l3": sorted(cache.l3),
            "hot_set": sorted(cache.hot_set),
            "candidate_pool": sorted(cache.candidate_pool),
            "probation": sorted(cache.probation),
        },
        "pointers": {
            pointer_id: {
                "path": pointer.path,
                "lines": [pointer.line_start, pointer.line_end],
                "state": pointer.coherence_state.value,
                "trust_score": pointer.trust_score,
                "pollution_score": pointer.pollution_score,
                "quality_state": pointer.quality_state.value,
                "usage_count": pointer.usage_count,
                "tags": list(pointer.tags),
            }
            for pointer_id, pointer in sorted(cache.l3.items())
        },
        "l1": {
            agent_id: [
                f"{pointer_id}@v{pointer.pointer_version}:{pointer.coherence_state.value}"
                for pointer_id, pointer in l1.items()
            ]
            for agent_id, l1 in sorted(cache.l1.items())
        },
        "comparison": {
            "tokens_with_cache": comparison.tokens_with_cache,
            "tokens_without_cache": comparison.tokens_without_cache,
            "saved_tokens": comparison.saved_tokens,
            "token_io_reduction": comparison.token_io_reduction,
            "saves_tokens": comparison.saves_tokens,
        },
    }


def parse_token_usage_line(line: str) -> dict[str, int | str] | None:
    if "codex.turn.token_usage." not in line:
        return None
    usage: dict[str, int | str] = {}
    turn_match = re.search(r"turn\.id=([^\s}]+)", line)
    if turn_match:
        usage["turn_id"] = turn_match.group(1)
    for field in TOKEN_USAGE_FIELDS:
        match = re.search(rf"codex\.turn\.token_usage\.{field}=(\d+)", line)
        if match:
            usage[field] = int(match.group(1))
    return usage if any(field in usage for field in TOKEN_USAGE_FIELDS) else None


def extract_token_usage(log_text: str) -> list[dict[str, int | str]]:
    return [
        usage
        for line in log_text.splitlines()
        if (usage := parse_token_usage_line(line)) is not None
    ]


def normalize_usage(usage: dict) -> dict[str, int]:
    input_tokens = int(usage.get("input_tokens", 0))
    cached_input_tokens = int(usage.get("cached_input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    reasoning_output_tokens = int(usage.get("reasoning_output_tokens", 0))
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "non_cached_input_tokens": int(usage.get("non_cached_input_tokens", input_tokens - cached_input_tokens)),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": int(usage.get("total_tokens", input_tokens + output_tokens)),
    }


def parse_codex_json_usage(stdout: str) -> dict[str, int] | None:
    latest: dict[str, int] | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            latest = normalize_usage(event["usage"])
    return latest


def build_codex_prompts() -> dict[str, str]:
    cache = build_cache()
    pointer_sections: list[str] = []
    for task in TASKS:
        pointer_id = task["pointer_id"]
        source_range = cache.open(pointer_id, agent_id="prompt_builder")
        pointer = cache.l3[pointer_id]
        pointer_sections.append(
            "\n".join(
                [
                    f"Task: {task['task']}",
                    f"Pointer: {pointer.pointer_id}",
                    f"Source: {pointer.path} lines {pointer.line_start}-{pointer.line_end}",
                    f"Tags: {', '.join(pointer.tags)}",
                    "Text:",
                    source_range.text,
                ]
            )
        )

    full_source_sections = [
        "\n".join(
            [
                f"Task: {task['task']}",
                "File:",
                task["path"],
                "Text:",
                cache.sources.get("random_shop", task["path"]).text,
            ]
        )
        for task in TASKS
    ]

    question = (
        "Return one compact JSON object with keys mode, orders, payments, and notifications. "
        "For each behavior, identify the relevant function and the key implementation detail."
    )
    with_cache = "\n\n".join(
        [
            "Use harness-cache. This is the cached source-pointer path.",
            "Do not read full files. Use only these verified pointer ranges.",
            *pointer_sections,
            question,
        ]
    )
    without_cache = "\n\n".join(
        [
            "This is the no-cache baseline path.",
            "Use the full source documents below.",
            *full_source_sections,
            question,
        ]
    )
    return {
        "with_harness_cache": with_cache,
        "without_harness_cache": without_cache,
    }


def codex_log_path() -> Path:
    codex_home = Path.home() / ".codex"
    return codex_home / "log" / "codex-tui.log"


def read_log_since(path: Path, offset: int) -> str:
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        handle.seek(offset)
        return handle.read()


def wait_for_token_usage(path: Path, offset: int, timeout_seconds: float = 5.0) -> dict[str, int | str]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, int | str] | None = None
    while time.monotonic() < deadline:
        usages = extract_token_usage(read_log_since(path, offset))
        if usages:
            latest = usages[-1]
            break
        time.sleep(0.2)
    if latest is None:
        raise RuntimeError(f"Codex token usage was not found in {path}")
    return latest


def run_codex_exec(
    label: str,
    prompt: str,
    codex_bin: str,
    codex_model: str | None,
    extra_args: list[str],
) -> CodexRun:
    log_path = codex_log_path()
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    with tempfile.TemporaryDirectory(prefix="harness-cache-codex-") as temp_dir:
        output_path = Path(temp_dir) / f"{label}.txt"
        command = [
            codex_bin,
            "exec",
            "--cd",
            str(ROOT),
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--json",
            "--output-last-message",
            str(output_path),
        ]
        if codex_model:
            command.extend(["--model", codex_model])
        command.extend(extra_args)
        command.append(prompt)
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        if completed.returncode != 0:
            raise RuntimeError(
                "\n".join(
                    [
                        f"Codex run failed for {label}: exit {completed.returncode}",
                        "Command:",
                        " ".join(command[:-1] + ["<prompt>"]),
                        "stdout:",
                        completed.stdout,
                        "stderr:",
                        completed.stderr,
                    ]
                )
            )
    usage = parse_codex_json_usage(completed.stdout)
    if usage is None:
        usage = wait_for_token_usage(log_path, log_offset, timeout_seconds=30.0)
    return CodexRun(label, prompt, command, usage, completed.stdout, completed.stderr)


def measure_codex_tokens(
    codex_bin: str,
    codex_model: str | None = None,
    extra_args: list[str] | None = None,
) -> dict:
    prompts = build_codex_prompts()
    runs = [
        run_codex_exec("with_harness_cache", prompts["with_harness_cache"], codex_bin, codex_model, extra_args or []),
        run_codex_exec("without_harness_cache", prompts["without_harness_cache"], codex_bin, codex_model, extra_args or []),
    ]
    by_label = {run.label: run.token_usage for run in runs}
    with_usage = by_label["with_harness_cache"]
    without_usage = by_label["without_harness_cache"]
    deltas = {
        field: int(without_usage.get(field, 0)) - int(with_usage.get(field, 0))
        for field in TOKEN_USAGE_FIELDS
    }
    return {
        "codex_log": str(codex_log_path()),
        "script_estimate": inspect_cache()["comparison"],
        "prompt_characters": {label: len(prompt) for label, prompt in prompts.items()},
        "runs": by_label,
        "saved_by_harness_cache": deltas,
    }


def print_measurement(measurement: dict) -> None:
    print("Codex measured token usage")
    print(f"Log source: {measurement['codex_log']}")
    print("")
    header = ["mode", *TOKEN_USAGE_FIELDS]
    rows = []
    for label in ("with_harness_cache", "without_harness_cache"):
        usage = measurement["runs"][label]
        rows.append([label, *[str(usage.get(field, 0)) for field in TOKEN_USAGE_FIELDS]])
    rows.append(["saved_by_harness_cache", *[str(measurement["saved_by_harness_cache"][field]) for field in TOKEN_USAGE_FIELDS]])
    widths = [max(len(row[index]) for row in [header, *rows]) for index in range(len(header))]
    print("  ".join(value.ljust(widths[index]) for index, value in enumerate(header)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
    print("")
    estimate = measurement["script_estimate"]
    print(
        "Demo script estimate: "
        f"{estimate['tokens_with_cache']} with cache vs "
        f"{estimate['tokens_without_cache']} without cache "
        f"({estimate['saved_tokens']} saved, {estimate['token_io_reduction']:.1%} reduction)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or inspect the Harness Cache demo.")
    parser.add_argument("--inspect", action="store_true", help="Print demo cache state as JSON.")
    parser.add_argument(
        "--measure-codex",
        action="store_true",
        help="Run live Codex with and without Harness Cache prompts and print measured token usage.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm that --measure-codex may run two live Codex model calls.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated Codex measurement prompts without calling Codex.",
    )
    parser.add_argument("--codex-bin", default="codex", help="Codex executable to call for --measure-codex.")
    parser.add_argument("--codex-model", default=None, help="Optional model override for codex exec.")
    parser.add_argument(
        "--codex-arg",
        action="append",
        default=[],
        help="Extra argument passed through to codex exec. Repeat for multiple args.",
    )
    parser.add_argument("--json-output", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    selected_modes = sum([args.inspect, args.measure_codex])
    if selected_modes > 1:
        parser.error("choose only one mode: --inspect or --measure-codex")

    if args.inspect:
        payload = inspect_cache()
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    if args.measure_codex:
        prompts = build_codex_prompts()
        if args.dry_run:
            print(json.dumps({"prompts": prompts}, indent=2, sort_keys=True))
            return
        if not args.yes:
            parser.error("--measure-codex runs two live Codex model calls; pass --yes to confirm")
        measurement = measure_codex_tokens(args.codex_bin, args.codex_model, args.codex_arg)
        if args.json_output:
            print(json.dumps(measurement, indent=2, sort_keys=True))
        else:
            print_measurement(measurement)
        return

    result = run_demo()
    if args.json_output:
        print(
            json.dumps(
                {
                    "tokens_without_cache": result.no_cache_tokens,
                    "tokens_with_cache": result.cache_tokens,
                    "saved_tokens": result.comparison.saved_tokens,
                    "token_io_reduction": result.comparison.token_io_reduction,
                    "saves_tokens": result.comparison.saves_tokens,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("Harness Cache token savings demo")
        print(f"No cache estimated tokens: {result.no_cache_tokens}")
        print(f"Harness Cache estimated tokens: {result.cache_tokens}")
        print(f"Saved estimated tokens: {result.comparison.saved_tokens}")
        print(f"Token I/O reduction: {result.comparison.token_io_reduction:.1%}")
        print(f"Saves tokens: {result.comparison.saves_tokens}")


if __name__ == "__main__":
    main()
