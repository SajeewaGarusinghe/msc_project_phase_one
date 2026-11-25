"""A4: the information boundary, enforced by file layout, Unix users and a scan.

Two halves, matching the build guide's two tests.

**Layout (A4 items 1-4, test A4-T1).** Three parties, three Unix users:

    sealer   owns the sealed-label store, mode 0700. Holds the labels and the variant key.
    runner   owns ``cases/`` and every case directory, mode 0711: a directory the arm can
             pass *through* to a path it was given, but cannot list.
    arm      runs every arm process. Belongs to neither of the other two groups.

An arm is launched in exactly one variant directory and is told nothing else. It cannot list
``cases/`` (other cases) or its own case directory (sibling variants), and it cannot open the
sealed store. Variant directory names are an HMAC of the variant under a key that lives in the
sealed store, so a sibling cannot be reached by *guessing* its name either: with a plain hash,
``sha256("F2-T2-high-7")`` is computable by anyone who knows the naming scheme, and the space of
family x threat x dose x seed is small enough to enumerate.

**Scan (A4 item 5, test A4-T2).** Every prompt, tool output and trajectory is searched for the
target's name near a word that would reveal the attack: "fake", "injected", a forgery word, or a
family code. Zero hits are required. The scan sees the *exact* tool outputs by replaying each
recorded tool call through RCAEval's ``CaseToolkit`` and checking the replayed size against the
``result_chars`` the trajectory recorded, so a replay that drifted from what the agent saw is
reported rather than silently scanned.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

SEALER_USER = os.environ.get("PROVRCA_SEALER_USER", "provrca-sealer")
ARM_USER = os.environ.get("PROVRCA_ARM_USER", "provrca-arm")
KEY_FILE = "variant.key"

# --------------------------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------------------------


def variant_dirname(variant: str, key: bytes) -> str:
    """The task-view name of an overlay variant: keyed, stable, and free of family or dose."""
    return hmac.new(key, variant.encode(), hashlib.sha256).hexdigest()[:16]


def _sudo(*argv: str, user: str | None = None, check: bool = True,
          capture: bool = False) -> subprocess.CompletedProcess:
    cmd = ["sudo", "-n"] + (["-u", user] if user else []) + ["--", *argv]
    return subprocess.run(cmd, check=check, text=True,
                          capture_output=capture, env={"PATH": "/usr/bin:/bin"})


def users_present(*names: str) -> bool:
    import pwd
    try:
        for n in names:
            pwd.getpwnam(n)
    except KeyError:
        return False
    return True


def sudo_available() -> bool:
    return shutil.which("sudo") is not None and subprocess.run(
        ["sudo", "-n", "true"], capture_output=True).returncode == 0


@dataclass
class SealedStore:
    """The label store: outside the case tree, owned by the sealer, mode 0700."""
    path: Path
    owner: str = SEALER_USER

    def create(self, key: bytes | None = None) -> bytes:
        """Create the store and its variant key. Returns the key for the builder, which runs as
        the sealer in production; the caller must not hand it to an arm."""
        key = key or secrets.token_bytes(32)
        self.path.mkdir(parents=True, exist_ok=False)
        (self.path / KEY_FILE).write_bytes(key)
        os.chmod(self.path / KEY_FILE, 0o600)
        _sudo("chown", "-R", f"{self.owner}:{self.owner}", str(self.path))
        _sudo("chmod", "0700", str(self.path))
        return key

    def write(self, name: str, data: bytes) -> None:
        """Write one sealed file as the sealer (the store is not writable by anyone else)."""
        subprocess.run(["sudo", "-n", "-u", self.owner, "--", "tee", str(self.path / name)],
                       input=data, stdout=subprocess.DEVNULL, check=True,
                       env={"PATH": "/usr/bin:/bin"})

    def remove(self) -> None:
        _sudo("rm", "-rf", "--", str(self.path), check=False)


def lay_out_case(cases_root: Path, case_id: str, variants: dict[str, dict[str, bytes]],
                 key: bytes) -> dict[str, Path]:
    """Write ``cases/<case>/<hmac(variant)>/<files>`` with the modes A4 needs.

    ``cases/`` and ``cases/<case>/`` are 0711: traversable, not listable. Variant directories and
    their files are world-readable, because the arm must read its own variant; what keeps it out
    of a sibling is that it can neither list the case directory nor compute a sibling's name.
    Returns ``{variant: path}`` for the runner, which is the only party that knows the mapping.
    """
    case_dir = cases_root / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for variant, files in variants.items():
        vdir = case_dir / variant_dirname(variant, key)
        vdir.mkdir(exist_ok=False)
        for name, data in files.items():
            (vdir / name).write_bytes(data)
            os.chmod(vdir / name, 0o644)
        os.chmod(vdir, 0o755)
        out[variant] = vdir
    os.chmod(case_dir, 0o711)
    os.chmod(cases_root, 0o711)
    return out


def launch_arm(argv: list[str], *, cwd: Path, user: str = ARM_USER,
               timeout: float = 60) -> subprocess.CompletedProcess:
    """Start an arm process as the arm user, inside its own variant directory, with an empty
    environment apart from PATH. This is the only way the runner may start an arm."""
    cmd = ["sudo", "-n", "-u", user, "--", "env", "-i", "PATH=/usr/bin:/bin",
           f"HOME={cwd}", *argv]
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)


# The probe an arm runs in A4-T1. It is a script rather than an import so it runs with the arm
# user's interpreter and no access to this package or its virtual environment.
PROBE = r"""
import json, os, sys
own, sealed_file, case_dir, cases_root, guessed = sys.argv[1:6]
def attempt(fn):
    try:
        fn(); return "allowed"
    except PermissionError:
        return "denied"
    except FileNotFoundError:
        return "not_found"
    except OSError as e:
        return "error:" + type(e).__name__
print(json.dumps({
    "read_own_variant": attempt(lambda: open(own, "rb").read(1)),
    "open_sealed_file": attempt(lambda: open(sealed_file, "rb").read(1)),
    "list_sealed_store": attempt(lambda: os.listdir(os.path.dirname(sealed_file))),
    "list_case_dir": attempt(lambda: os.listdir(case_dir)),
    "list_cases_root": attempt(lambda: os.listdir(cases_root)),
    "open_guessed_sibling": attempt(lambda: open(guessed, "rb").read(1)),
}))
"""


# --------------------------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------------------------

# Words that would tell the agent it is looking at an attack. "injected" is the guide's word;
# the forgery words are added because they say the same thing. "injection" is deliberately NOT
# here: RCAEval's own prompt says "a fault was injected" and its tools speak of the "injection
# time", which names the real fault, not the overlay, and is legitimately visible.
REVEAL_WORDS = re.compile(r"\b(fake[sd]?|faked|injected|fabricat\w*|forg(?:e|ed|ery|eries)|"
                          r"spoof\w*|overlay)\b", re.IGNORECASE)
# Family codes, case-sensitive with word boundaries so hex ids and metric names do not match.
FAMILY_CODES = re.compile(r"(?<![A-Za-z0-9_])(F[1-4]c?|FSc?|A1|AF[12])(?![A-Za-z0-9_])")
WINDOW = 120   # characters either side of the target's name
# RCAEval's own wording about the *real* chaos fault, which its prompt hands the agent ("a fault
# was injected into the ... system at <time>") and agents echo in their answers ("the fault was
# injected into currencyservice", "the injected fault targets ..."). It names the real fault, not
# the overlay. Masked before matching and listed in the evidence, so the exemption is narrow and
# visible: "injected" beside anything other than "fault" -- injected logs, spans, records --
# still counts. The unmasked scan of the pilot is kept as evidence/a4_t2_pilot_strict.json.
BENIGN_PHRASES = re.compile(
    r"\b(?:a\s+)?(?:\w+/)?\w*\s?fault\s+(?:was\s+|is\s+|has\s+been\s+)?injected\b"
    r"|\binjected\s+fault\b", re.IGNORECASE)


@dataclass(frozen=True)
class Hit:
    case_id: str
    source: str        # "prompt", "tool_output:<seq>:<tool>" or "trajectory:<line>"
    target: str
    word: str
    context: str


def scan_text(text: str, target: str, *, case_id: str = "", source: str = "",
              window: int = WINDOW) -> list[Hit]:
    """Every place ``target`` appears within ``window`` characters of a revealing word or a
    family code. Matching on the target name alone is case-insensitive; service names appear in
    lower case in RCAEval but agents write them capitalised."""
    text = BENIGN_PHRASES.sub(lambda m: "#" * len(m.group(0)), text)
    hits: list[Hit] = []
    for m in re.finditer(re.escape(target), text, re.IGNORECASE):
        lo, hi = max(0, m.start() - window), min(len(text), m.end() + window)
        ctx = text[lo:hi]
        for pat in (REVEAL_WORDS, FAMILY_CODES):
            for w in pat.finditer(ctx):
                hits.append(Hit(case_id, source, target, w.group(0), ctx.replace("\n", " ")))
    return hits


@dataclass
class CaseAudit:
    case_id: str
    target: str
    family: str
    prompts_scanned: int = 0
    tool_outputs_scanned: int = 0
    tool_output_chars: int = 0
    trajectory_lines_scanned: int = 0
    replay_mismatches: list[dict] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)


def _trajectory_texts(path: Path) -> Iterator[tuple[int, str]]:
    """Each trajectory line, as the text an outsider could read: the whole JSON record."""
    with path.open() as f:
        for i, line in enumerate(f, 1):
            yield i, line


def _tool_calls(path: Path) -> Iterator[dict]:
    with path.open() as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("type") == "tool_call":
                yield rec


def audit_case(*, case_id: str, target: str, family: str, case_dir: Path,
               trajectory: Path, prompt_version: str = "2") -> CaseAudit:
    """Scan one case's prompt, replayed tool outputs and trajectory for the target."""
    from RCAEval.agent.prompt import full_prompt
    from RCAEval.agent.toolkit import CaseToolkit

    audit = CaseAudit(case_id, target, family)
    kit = CaseToolkit(str(case_dir))

    prompt = full_prompt(kit.task, version=prompt_version)
    audit.prompts_scanned = 1
    audit.hits += scan_text(prompt, target, case_id=case_id, source="prompt")

    for rec in _tool_calls(trajectory):
        result = kit.call(rec["tool"], rec.get("args") or {})
        text = json.dumps(result, default=str)
        if len(text) != rec.get("result_chars"):
            audit.replay_mismatches.append({"seq": rec["seq"], "tool": rec["tool"],
                                            "recorded": rec.get("result_chars"),
                                            "replayed": len(text)})
        audit.tool_outputs_scanned += 1
        audit.tool_output_chars += len(text)
        audit.hits += scan_text(text, target, case_id=case_id,
                                source=f"tool_output:{rec['seq']}:{rec['tool']}")

    for i, text in _trajectory_texts(trajectory):
        audit.trajectory_lines_scanned += 1
        audit.hits += scan_text(text, target, case_id=case_id, source=f"trajectory:{i}")
    return audit


def audit_pilot(*, labels_jsonl: Path, cases_root: Path, trajectories: Path,
                case_ids: Iterable[str] | None = None) -> list[CaseAudit]:
    """A4-T2 over the G0 pilot: one audit per labelled case that has a trajectory."""
    labels = [json.loads(l) for l in labels_jsonl.read_text().splitlines() if l.strip()]
    wanted = set(case_ids) if case_ids is not None else None
    audits = []
    for lab in labels:
        cid = lab["case_id"]
        if wanted is not None and cid not in wanted:
            continue
        traj = trajectories / f"{cid}.jsonl"
        if not traj.exists():
            raise FileNotFoundError(f"no trajectory for {cid}: the scan must cover every run")
        audits.append(audit_case(case_id=cid, target=lab["target"], family=lab["family"],
                                 case_dir=cases_root / cid, trajectory=traj))
    return audits


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="A4-T2: scan the pilot for target leakage")
    p.add_argument("--labels", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--trajectories", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True, help="JSON summary to write")
    a = p.parse_args(argv)

    audits = audit_pilot(labels_jsonl=a.labels, cases_root=a.cases, trajectories=a.trajectories)
    hits = [h.__dict__ for au in audits for h in au.hits]
    mismatches = [{"case_id": au.case_id, **m} for au in audits for m in au.replay_mismatches]
    summary = {
        "test": "A4-T2",
        "cases": len(audits),
        "families": sorted({au.family for au in audits}),
        "prompts_scanned": sum(au.prompts_scanned for au in audits),
        "tool_outputs_scanned": sum(au.tool_outputs_scanned for au in audits),
        "tool_output_chars": sum(au.tool_output_chars for au in audits),
        "trajectory_lines_scanned": sum(au.trajectory_lines_scanned for au in audits),
        "window_chars": WINDOW,
        "reveal_words": REVEAL_WORDS.pattern,
        "family_codes": FAMILY_CODES.pattern,
        "benign_phrases_masked": BENIGN_PHRASES.pattern,
        "replay_mismatches": mismatches,
        "hits": hits,
        "passed": not hits and not mismatches,
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"A4-T2: {len(audits)} cases, {summary['tool_outputs_scanned']} tool outputs "
          f"({summary['tool_output_chars']:,} chars), {summary['trajectory_lines_scanned']} "
          f"trajectory lines; {len(hits)} hits, {len(mismatches)} replay mismatches -> "
          f"{'PASS' if summary['passed'] else 'FAIL'}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
