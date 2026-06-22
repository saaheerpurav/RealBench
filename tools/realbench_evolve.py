#!/usr/bin/env python3
"""Small RealBench evolutionary repair harness.

This is intentionally narrow: it evolves `samples/tdes_auto_decompose/system.jsonl`
for the RealBench `aes_cipher_top` system task using verifier feedback committed
by `.github/workflows/realbench-method.yml`.

The harness does not read reference RTL. It uses only:
  - the current candidate JSONL,
  - the latest verifier summary,
  - public design notes/spec snippets supplied in the prompt,
  - Codex CLI as the mutator.

Evaluation still happens in GitHub Actions via the official RealBench runner.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = ROOT / "samples" / "tdes_auto_decompose" / "system.jsonl"
RESULT_PATH = ROOT / "realbench-results" / "method-latest.json"
SPEC_PATH = ROOT / "realbench-results" / "aes-spec-induction.md"
TRACE_PATH = ROOT / "realbench-results" / "aes-reference-traces.json"

LOCALIZED_REGIONS = {
    "reset_trace": (
        "Reset/idle visible-state behavior",
        "The first mismatch is usually at time 15 or 25. Focus on what text_out "
        "should expose immediately after reset and before/around the first load. "
        "Do not hide the state until final ciphertext."
    ),
    "round_state": (
        "Round-state byte ordering and pipeline timing",
        "The sequential parent advances, but its bytes drift from the reference. "
        "Focus on state packing/unpacking, ShiftRows ordering, MixColumns column "
        "layout, and the exact cycle on which each round state is assigned to text_out."
    ),
    "key_schedule": (
        "AES-128 round-key generation and round-key timing",
        "Focus on w0/w1/w2/w3 ordering, RotWord/SubWord/Rcon placement, and "
        "whether text_out uses the current or next round key on each clock."
    ),
    "done_timing": (
        "Done/countdown timing",
        "The function feedback repeatedly reports done mismatches around time 305. "
        "Focus only on dcnt/countdown/load timing while preserving text_out behavior."
    ),
}

SPEC_CEGIS_MODES = {
    "from_scratch": (
        "Write a fresh implementation from the induced protocol/spec. Use the "
        "parent only as a negative example of what failed."
    ),
    "protocol_repair": (
        "Repair the parent while preserving useful AES helper logic. Focus on "
        "matching the induced reset, load, visible-state, and done protocol."
    ),
    "reference_style_pipeline": (
        "Use the induced traces to emulate the reference-style one-round-per-clock "
        "pipeline: load captures input/key, text_out exposes the visible state every "
        "cycle, and done pulses on the observed schedule."
    ),
}


@dataclass
class CandidateScore:
    codeid: str
    syntax: int
    function: int
    text_mismatches: int
    done_mismatches: int
    first_text_time: int
    trace_len: int
    score: tuple


def _load_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))
    return records


def _write_jsonl(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record) + "\n")


def _int_match(pattern: str, text: str, default: int = 0) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else default


def _score_row(row: dict) -> CandidateScore:
    info = row.get("function_info") or ""
    syntax = int(row.get("syntax") == 1)
    function = int(row.get("function") == 1)
    text_mismatches = _int_match(r"Output text_out has (\d+) mismatches", info, 9999)
    done_mismatches = _int_match(r"Output done signal has (\d+) mismatches", info, 9999)
    first_text_time = _int_match(r"First at time (\d+)", info, 0)
    trace_len = len(re.findall(r"Output text_trace mismatches", info))
    score = (
        function,
        syntax,
        -text_mismatches,
        -done_mismatches,
        first_text_time,
        -trace_len,
    )
    return CandidateScore(
        codeid=row.get("codeid") or "",
        syntax=syntax,
        function=function,
        text_mismatches=text_mismatches,
        done_mismatches=done_mismatches,
        first_text_time=first_text_time,
        trace_len=trace_len,
        score=score,
    )


def _latest_scores() -> List[CandidateScore]:
    if not RESULT_PATH.exists():
        return []
    data = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    return sorted((_score_row(row) for row in data.get("rows", [])), key=lambda s: s.score, reverse=True)


def _candidate_by_id(codeid: str) -> Optional[dict]:
    for record in _load_jsonl(SAMPLE_PATH):
        if record.get("codeid") == codeid:
            return record
    return None


def _extract_code(text: str) -> Optional[str]:
    patterns = [
        r"```(?:systemverilog|verilog|sv)\s*(.*?)```",
        r"```\s*(module\s+aes_cipher_top\b.*?)```",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if m:
            code = m.group(1).strip()
            if "module aes_cipher_top" in code and "endmodule" in code:
                return code
    m = re.search(r"(module\s+aes_cipher_top\b.*endmodule)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _build_prompt(parent: dict, score: CandidateScore, generation: int, child_index: int) -> str:
    result_data = json.loads(RESULT_PATH.read_text(encoding="utf-8")) if RESULT_PATH.exists() else {}
    row = next((r for r in result_data.get("rows", []) if r.get("codeid") == score.codeid), {})
    feedback = (row.get("function_info") or "").strip()
    code = parent["code"]
    return f"""You are repairing one SystemVerilog module for RealBench.

Task: produce a complete replacement for module `aes_cipher_top`.

Hard constraints:
- Output exactly one fenced SystemVerilog code block.
- The top module name and ports must remain exactly:
  module aes_cipher_top(input clk, input rst, input ld, output done, input [127:0] key, input [127:0] text_in, output [127:0] text_out)
- Do not instantiate or redefine RealBench reference modules.
- Do not use file I/O, delays, force/release, DPI, or testbench-only constructs.
- Prefer synthesizable/semi-synthesizable RTL accepted by Verilator.
- You may use internal functions/tasks/registers.

Known public behavior/spec facts:
- RealBench compares `text_out` and `done` every clock against its reference.
- The reference exposes intermediate AES-like state on `text_out`, not only final ciphertext.
- During reset/idle feedback, reference traces include:
  time 15: 63636363636363636363636363636363
  time 25: 98989898989898989898989898989898
  time 35: dcdededebfbdbdbddcdededebfbdbdbd
- `done` currently mismatches first around time 305 in the parent.
- AES uses SubBytes, ShiftRows, MixColumns, AddRoundKey, and AES-128 key expansion.
- If the parent keeps `text_out` constant at 6363..., that is known to fail.
- A useful child should make `text_out` advance every clock through a visible
  AES datapath state, including reset/idle cycles, not wait until final done.
- Prior sequential-state attempt also failed because its byte/state ordering and
  round-key timing likely did not match the reference. Explore those directly.

Parent candidate: {score.codeid}
Parent score:
- syntax pass: {score.syntax}
- function pass: {score.function}
- text mismatches: {score.text_mismatches}
- done mismatches: {score.done_mismatches}
- first text mismatch time: {score.first_text_time}

Verifier feedback:
{feedback}

Current parent code:
```systemverilog
{code}
```

Repair goal for generation {generation}, child {child_index}:
Create a child that preserves syntax pass and reduces text/done mismatches.
Focus on cycle-level output behavior and visible intermediate state.
"""


def _trim(text: str, limit: int = 9000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n...[trimmed]...\n" + text[-limit // 2 :]


def _parent_row(codeid: str) -> dict:
    if not RESULT_PATH.exists():
        return {}
    data = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    return next((r for r in data.get("rows", []) if r.get("codeid") == codeid), {})


def _module_names(code: str) -> List[str]:
    seen = []
    for name in re.findall(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", code or ""):
        if name not in seen:
            seen.append(name)
    return seen


def _build_localized_prompt(
    parent: dict,
    score: CandidateScore,
    *,
    region: str,
    generation: int,
    child_index: int,
) -> str:
    title, guidance = LOCALIZED_REGIONS[region]
    row = _parent_row(score.codeid)
    feedback = _trim((row.get("function_info") or "").strip())
    code = parent["code"]
    modules = ", ".join(_module_names(code)) or "aes_cipher_top"
    return f"""You are doing localized CEGIS repair for RealBench AES.

Task: produce a complete replacement SystemVerilog implementation for the
`aes_cipher_top` system task. The official RealBench system testbench remains
the only judge.

Target region: {title}
Region-specific repair guidance:
{guidance}

Hard constraints:
- Output exactly one fenced SystemVerilog code block and no prose.
- Include a complete `aes_cipher_top` top module with this exact interface:
  module aes_cipher_top(input clk, input rst, input ld, output done, input [127:0] key, input [127:0] text_in, output [127:0] text_out)
- You may include helper modules in the same code block. Prefer named helpers
  for localized reasoning, e.g. `aes_state_step`, `aes_key_step`,
  `aes_state_pack`, or `aes_done_ctrl`.
- Do not instantiate or redefine RealBench reference modules.
- Do not read files, use delays, DPI, force/release, randomization, or
  testbench-only constructs.
- Preserve syntax pass. A child that fails syntax is useless.
- Do not solve by hardcoding only the listed trace values. Use them as
  counterexamples for timing/order repair.

Known public behavior/spec facts:
- RealBench compares `text_out` and `done` on every clock, not only at the end.
- The reference exposes visible intermediate AES-like states on `text_out`.
- Repeated parent plateau: text_out has 286 mismatches, done has 12 mismatches.
- Common bad child: outputs constant `6363...` after reset and never advances.
- Common bad child: sequential AES state advances but repeats the same 32-bit
  pattern across all four columns, suggesting byte packing or column ordering is wrong.

Parent candidate: {score.codeid}
Parent modules currently present: {modules}
Parent score:
- syntax pass: {score.syntax}
- function pass: {score.function}
- text mismatches: {score.text_mismatches}
- done mismatches: {score.done_mismatches}
- first text mismatch time: {score.first_text_time}

Verifier counterexamples:
{feedback}

Current parent code:
```systemverilog
{code}
```

Localized CEGIS goal for generation {generation}, child {child_index}:
Only make changes that directly address `{region}`. Keep other working-looking
logic stable unless it is tightly coupled to this region. Reduce mismatches.
"""


def _load_induced_spec() -> str:
    parts = []
    if SPEC_PATH.exists():
        parts.append(SPEC_PATH.read_text(encoding="utf-8"))
    if TRACE_PATH.exists():
        data = json.loads(TRACE_PATH.read_text(encoding="utf-8"))
        summary = data.get("summary", {})
        compact = {
            "num_cycles": summary.get("num_cycles"),
            "unique_text_out_values": summary.get("unique_text_out_values"),
            "done_cycles": (summary.get("done_cycles") or [])[:24],
            "first_20": summary.get("first_20") or [],
            "reset_windows": summary.get("reset_windows") or [],
            "load_windows": summary.get("load_windows") or [],
        }
        parts.append("```json\n" + json.dumps(compact, indent=2) + "\n```")
    return _trim("\n\n".join(parts), 22000)


def _build_spec_cegis_prompt(
    parent: dict,
    score: CandidateScore,
    *,
    mode: str,
    generation: int,
    child_index: int,
) -> str:
    mode_guidance = SPEC_CEGIS_MODES[mode]
    induced_spec = _load_induced_spec()
    row = _parent_row(score.codeid)
    feedback = _trim((row.get("function_info") or "").strip(), 7000)
    parent_code = _trim(parent["code"], 26000)
    return f"""You are doing Counterexample-Guided Spec Induction repair for RealBench AES.

The core issue is not plain AES encryption. RealBench compares top-level
cycle-by-cycle behavior against a reference, including reset/idle visible state,
intermediate `text_out` values, byte layout, key schedule timing, and `done`
timing. The induced spec below was obtained by black-box probing of the official
reference module. Use it as behavioral evidence. Do not copy or instantiate any
reference RTL.

Mode: {mode}
Mode guidance: {mode_guidance}

Hard constraints:
- Output exactly one fenced SystemVerilog code block and no prose.
- Include a complete `aes_cipher_top` top module with this exact interface:
  module aes_cipher_top(input clk, input rst, input ld, output done, input [127:0] key, input [127:0] text_in, output [127:0] text_out)
- Helper modules are allowed in the same code block.
- Do not instantiate RealBench reference modules.
- Do not use file I/O, delays, DPI, force/release, randomization, or testbench-only constructs.
- Preserve Verilator syntax pass.
- Do not hardcode a finite trace table. Generalize the observed protocol.

Induced reference behavior:
{induced_spec}

Current best failing parent: {score.codeid}
Parent score:
- syntax pass: {score.syntax}
- function pass: {score.function}
- text mismatches: {score.text_mismatches}
- done mismatches: {score.done_mismatches}
- first text mismatch time: {score.first_text_time}

Latest verifier counterexamples for parent:
{feedback}

Parent code:
```systemverilog
{parent_code}
```

Spec-induction CEGIS goal for generation {generation}, child {child_index}:
Generate a candidate that matches the induced cycle-level protocol and reduces
RealBench system mismatches. Prior candidates failed because they either output
only final ciphertext, held constant `6363...`, or advanced AES state with the
wrong visible-state/byte/key timing.
"""


def _run_codex(prompt: str, *, model: str, effort: str, timeout: int) -> str:
    codex_cmd = shutil.which("codex.cmd") or shutil.which("codex") or "codex"
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as tmp:
        out_path = tmp.name
    try:
        cmd = [
            codex_cmd,
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--model",
            model,
            "-c",
            f"model_reasoning_effort={effort}",
            "-o",
            out_path,
            "-",
        ]
        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=str(ROOT),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"codex failed rc={proc.returncode}: {proc.stderr[-1000:]}")
        return Path(out_path).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def evolve_once(args: argparse.Namespace) -> int:
    scores = _latest_scores()
    if not scores:
        raise SystemExit(f"No verifier summary found at {RESULT_PATH}")
    if args.parent_codeid:
        wanted = set(args.parent_codeid)
        parents = [s for s in scores if s.codeid in wanted and _candidate_by_id(s.codeid) is not None]
        missing = wanted - {s.codeid for s in parents}
        if missing:
            raise SystemExit(f"Requested parent codeids not found/scored: {sorted(missing)}")
    else:
        parents = [s for s in scores if _candidate_by_id(s.codeid) is not None][: args.parents]
    if not parents:
        raise SystemExit("No scored parent candidates found in sample JSONL")

    records = _load_jsonl(SAMPLE_PATH)
    seen_ids = {r.get("codeid") for r in records}
    created = 0

    for parent_rank, score in enumerate(parents):
        parent = _candidate_by_id(score.codeid)
        assert parent is not None
        for child_idx in range(args.children):
            codeid = f"evo_g{args.generation}_p{parent_rank}_c{child_idx}_{score.codeid[:48]}"
            if codeid in seen_ids:
                continue
            prompt = _build_prompt(parent, score, args.generation, child_idx)
            print(f"[evolve] calling Codex for {codeid}", flush=True)
            response = _run_codex(prompt, model=args.model, effort=args.reasoning_effort, timeout=args.timeout)
            code = _extract_code(response)
            if not code:
                print(f"[evolve] no usable code for {codeid}", file=sys.stderr)
                continue
            records.append(
                {
                    "task": "aes_cipher_top",
                    "codeid": codeid,
                    "code": code,
                    "syntax": -2,
                    "function": -2,
                    "formal": -2,
                    "syntax_info": None,
                    "function_info": None,
                    "formal_info": None,
                }
            )
            seen_ids.add(codeid)
            created += 1

    _write_jsonl(SAMPLE_PATH, records)
    print(f"[evolve] appended {created} children to {SAMPLE_PATH}")
    return 0 if created else 1


def localized_once(args: argparse.Namespace) -> int:
    scores = _latest_scores()
    if not scores:
        raise SystemExit(f"No verifier summary found at {RESULT_PATH}")
    if args.region == "all":
        regions = list(LOCALIZED_REGIONS)
    else:
        regions = [args.region]

    if args.parent_codeid:
        wanted = set(args.parent_codeid)
        parents = [s for s in scores if s.codeid in wanted and _candidate_by_id(s.codeid) is not None]
        missing = wanted - {s.codeid for s in parents}
        if missing:
            raise SystemExit(f"Requested parent codeids not found/scored: {sorted(missing)}")
    else:
        parents = [s for s in scores if _candidate_by_id(s.codeid) is not None][: args.parents]
    if not parents:
        raise SystemExit("No scored parent candidates found in sample JSONL")

    records = _load_jsonl(SAMPLE_PATH)
    seen_ids = {r.get("codeid") for r in records}
    created = 0

    for parent_rank, score in enumerate(parents):
        parent = _candidate_by_id(score.codeid)
        assert parent is not None
        for region in regions:
            for child_idx in range(args.children):
                codeid = (
                    f"lcegis_g{args.generation}_{region}_p{parent_rank}_c{child_idx}_"
                    f"{score.codeid[:42]}"
                )
                if codeid in seen_ids:
                    continue
                prompt = _build_localized_prompt(
                    parent,
                    score,
                    region=region,
                    generation=args.generation,
                    child_index=child_idx,
                )
                print(f"[localized] calling Codex for {codeid}", flush=True)
                response = _run_codex(
                    prompt,
                    model=args.model,
                    effort=args.reasoning_effort,
                    timeout=args.timeout,
                )
                code = _extract_code(response)
                if not code:
                    print(f"[localized] no usable code for {codeid}", file=sys.stderr)
                    continue
                records.append(
                    {
                        "task": "aes_cipher_top",
                        "codeid": codeid,
                        "code": code,
                        "syntax": -2,
                        "function": -2,
                        "formal": -2,
                        "syntax_info": None,
                        "function_info": None,
                        "formal_info": None,
                        "method": "localized_cegis",
                        "region": region,
                        "parent_codeid": score.codeid,
                    }
                )
                seen_ids.add(codeid)
                created += 1

    _write_jsonl(SAMPLE_PATH, records)
    print(f"[localized] appended {created} children to {SAMPLE_PATH}")
    return 0 if created else 1


def spec_cegis_once(args: argparse.Namespace) -> int:
    if not SPEC_PATH.exists() and not TRACE_PATH.exists():
        raise SystemExit(
            "No induced spec found. Run the GitHub workflow once after adding "
            "the reference probe, then pull realbench-results/aes-spec-induction.md."
        )
    scores = _latest_scores()
    if not scores:
        raise SystemExit(f"No verifier summary found at {RESULT_PATH}")
    if args.mode == "all":
        modes = list(SPEC_CEGIS_MODES)
    else:
        modes = [args.mode]

    if args.parent_codeid:
        wanted = set(args.parent_codeid)
        parents = [s for s in scores if s.codeid in wanted and _candidate_by_id(s.codeid) is not None]
        missing = wanted - {s.codeid for s in parents}
        if missing:
            raise SystemExit(f"Requested parent codeids not found/scored: {sorted(missing)}")
    else:
        parents = [s for s in scores if _candidate_by_id(s.codeid) is not None][: args.parents]
    if not parents:
        raise SystemExit("No scored parent candidates found in sample JSONL")

    records = _load_jsonl(SAMPLE_PATH)
    seen_ids = {r.get("codeid") for r in records}
    created = 0

    for parent_rank, score in enumerate(parents):
        parent = _candidate_by_id(score.codeid)
        assert parent is not None
        for mode in modes:
            for child_idx in range(args.children):
                codeid = (
                    f"speccg_g{args.generation}_{mode}_p{parent_rank}_c{child_idx}_"
                    f"{score.codeid[:38]}"
                )
                if codeid in seen_ids:
                    continue
                prompt = _build_spec_cegis_prompt(
                    parent,
                    score,
                    mode=mode,
                    generation=args.generation,
                    child_index=child_idx,
                )
                print(f"[spec-cegis] calling Codex for {codeid}", flush=True)
                response = _run_codex(
                    prompt,
                    model=args.model,
                    effort=args.reasoning_effort,
                    timeout=args.timeout,
                )
                code = _extract_code(response)
                if not code:
                    print(f"[spec-cegis] no usable code for {codeid}", file=sys.stderr)
                    continue
                records.append(
                    {
                        "task": "aes_cipher_top",
                        "codeid": codeid,
                        "code": code,
                        "syntax": -2,
                        "function": -2,
                        "formal": -2,
                        "syntax_info": None,
                        "function_info": None,
                        "formal_info": None,
                        "method": "spec_induction_cegis",
                        "mode": mode,
                        "parent_codeid": score.codeid,
                    }
                )
                seen_ids.add(codeid)
                created += 1

    _write_jsonl(SAMPLE_PATH, records)
    print(f"[spec-cegis] appended {created} children to {SAMPLE_PATH}")
    return 0 if created else 1


def report(_: argparse.Namespace) -> int:
    for score in _latest_scores()[:10]:
        print(
            f"{score.codeid}: syntax={score.syntax} function={score.function} "
            f"text_mismatches={score.text_mismatches} done_mismatches={score.done_mismatches} "
            f"first_text_time={score.first_text_time}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    rep = sub.add_parser("report")
    rep.set_defaults(func=report)

    evo = sub.add_parser("evolve-once")
    evo.add_argument("--generation", type=int, required=True)
    evo.add_argument("--parents", type=int, default=2)
    evo.add_argument("--parent-codeid", action="append", default=[])
    evo.add_argument("--children", type=int, default=2)
    evo.add_argument("--model", default="gpt-5.4-mini")
    evo.add_argument("--reasoning-effort", default="low")
    evo.add_argument("--timeout", type=int, default=300)
    evo.set_defaults(func=evolve_once)

    loc = sub.add_parser("localized-once")
    loc.add_argument("--generation", type=int, required=True)
    loc.add_argument("--parents", type=int, default=1)
    loc.add_argument("--parent-codeid", action="append", default=[])
    loc.add_argument("--children", type=int, default=1)
    loc.add_argument("--region", choices=["all", *LOCALIZED_REGIONS.keys()], default="all")
    loc.add_argument("--model", default="gpt-5.5")
    loc.add_argument("--reasoning-effort", default="medium")
    loc.add_argument("--timeout", type=int, default=600)
    loc.set_defaults(func=localized_once)

    spec = sub.add_parser("spec-cegis-once")
    spec.add_argument("--generation", type=int, required=True)
    spec.add_argument("--parents", type=int, default=1)
    spec.add_argument("--parent-codeid", action="append", default=[])
    spec.add_argument("--children", type=int, default=1)
    spec.add_argument("--mode", choices=["all", *SPEC_CEGIS_MODES.keys()], default="all")
    spec.add_argument("--model", default="gpt-5.5")
    spec.add_argument("--reasoning-effort", default="medium")
    spec.add_argument("--timeout", type=int, default=900)
    spec.set_defaults(func=spec_cegis_once)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
