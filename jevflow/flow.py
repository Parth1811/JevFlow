"""Flow file loading and validation (SPEC 1, 10.1, 10.3, 10.5).

A flow is ``.jevflow/flow.json`` in the target project. Validation is strict
so a typo fails loudly at load time instead of silently changing behaviour.

Dependency rule: a phase WITHOUT a ``depends_on`` key depends on the phase
listed before it (linear default). ``depends_on: []`` makes it a root.
An explicit list replaces the implicit dependency.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

SUPPORTED_SCHEMA_VERSIONS = (1,)
PHASE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
RESERVED_PHASE_IDS = frozenset({"unclear"})

TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "flow_version", "goal", "phases", "limits", "privacy",
     "mode", "gates", "notify"}
)
PHASE_KEYS = frozenset(
    {"id", "name", "done_when", "check", "depends_on", "loop", "on_fail",
     "dynamic", "side_effect"}
)
MODES = ("observe", "warn", "enforce")

DEFAULT_LIMITS: Dict[str, Any] = {
    "max_blocks_per_session": 6,
    "max_restarts": 5,
    "max_total_minutes": 90,
    "hang_minutes": 10,
    "max_jev_calls": 200,
    "check_timeout_s": 120,
    "state_char_budget": 12000,
    "confidence": {"auto": 0.80, "review": 0.50, "flag": 0.70},
}
DEFAULT_PRIVACY: Dict[str, Any] = {"send_diff": False}


class FlowError(ValueError):
    """The flow file is missing, unreadable, or invalid."""


@dataclass(frozen=True)
class Loop:
    max_iterations: int
    until: str


@dataclass(frozen=True)
class Phase:
    id: str
    name: str
    done_when: str
    check: Optional[str] = None
    depends_on: Tuple[str, ...] = ()
    loop: Optional[Loop] = None
    on_fail: Optional[str] = None
    dynamic: bool = False
    side_effect: bool = False


@dataclass(frozen=True)
class Flow:
    goal: str
    phases: Tuple[Phase, ...]
    schema_version: int = 1
    flow_version: str = "1"
    limits: Mapping[str, Any] = field(default_factory=dict)
    privacy: Mapping[str, Any] = field(default_factory=dict)
    mode: str = "enforce"
    gates: Mapping[str, Any] = field(default_factory=dict)
    notify: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ids(self) -> List[str]:
        return [p.id for p in self.phases]

    def phase(self, phase_id: str) -> Phase:
        for p in self.phases:
            if p.id == phase_id:
                return p
        raise KeyError(phase_id)

    def next_phase(self, phase_id: str) -> Optional[str]:
        """Next phase in declaration order, or None if last."""
        ids = self.ids
        i = ids.index(phase_id)
        return ids[i + 1] if i + 1 < len(ids) else None

    def topo_order(self) -> List[str]:
        return _topo_order(self.phases)

    @property
    def branch_only(self) -> frozenset:
        """on_fail targets that no phase depends on (for example ``debug``).

        They run only when routed to by ``on_fail``; they are never offered by
        normal eligibility and are not required for goal completion.
        """
        targets = {p.on_fail for p in self.phases if p.on_fail}
        depended = {d for p in self.phases for d in p.depends_on}
        return frozenset(targets - depended)

    def required(self) -> List[str]:
        """Phases that must be done for the goal to be complete."""
        bo = self.branch_only
        return [p.id for p in self.phases if p.id not in bo]

    def eligible(self, phase_status: Mapping[str, str]) -> List[str]:
        """Phases not yet done whose dependencies are all done, in declaration
        order. Branch-only phases are excluded."""
        bo = self.branch_only
        out = []
        for p in self.phases:
            if phase_status.get(p.id) == "done" or p.id in bo:
                continue
            if all(phase_status.get(d) == "done" for d in p.depends_on):
                out.append(p.id)
        return out


def _req_str(obj: Mapping[str, Any], key: str, where: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str) or not val.strip():
        raise FlowError(f"{where}: '{key}' must be a non-empty string")
    return val.strip()


def _opt_str(obj: Mapping[str, Any], key: str, where: str) -> Optional[str]:
    val = obj.get(key)
    if val is None:
        return None
    if not isinstance(val, str) or not val.strip():
        raise FlowError(f"{where}: '{key}' must be a non-empty string or null")
    return val.strip()


def _unit(val: Any, where: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0.0 <= val <= 1.0:
        raise FlowError(f"{where} must be a number in [0, 1]")
    return float(val)


def _pos_int(val: Any, where: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or val < 1:
        raise FlowError(f"{where} must be an integer >= 1")
    return val


def _parse_limits(raw: Any) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FlowError("limits must be an object")
    unknown = set(raw) - set(DEFAULT_LIMITS)
    if unknown:
        raise FlowError(f"limits: unknown keys {sorted(unknown)}")
    out: Dict[str, Any] = {k: v for k, v in DEFAULT_LIMITS.items() if k != "confidence"}
    for k, v in raw.items():
        if k == "confidence":
            continue
        out[k] = _pos_int(v, f"limits.{k}")
    conf = dict(DEFAULT_LIMITS["confidence"])
    rc = raw.get("confidence", {})
    if not isinstance(rc, dict):
        raise FlowError("limits.confidence must be an object")
    unknown = set(rc) - set(conf)
    if unknown:
        raise FlowError(f"limits.confidence: unknown keys {sorted(unknown)}")
    for k, v in rc.items():
        conf[k] = _unit(v, f"limits.confidence.{k}")
    if conf["review"] > conf["auto"]:
        raise FlowError("limits.confidence: review must be <= auto")
    out["confidence"] = conf
    return out


def _parse_phase(raw: Any, index: int, prev_id: Optional[str]) -> Phase:
    where = f"phases[{index}]"
    if not isinstance(raw, dict):
        raise FlowError(f"{where} must be an object")
    unknown = set(raw) - PHASE_KEYS
    if unknown:
        raise FlowError(f"{where}: unknown keys {sorted(unknown)}")
    pid = _req_str(raw, "id", where)
    if not PHASE_ID_RE.match(pid):
        raise FlowError(f"{where}: id {pid!r} must match {PHASE_ID_RE.pattern}")
    if pid in RESERVED_PHASE_IDS:
        raise FlowError(f"{where}: id {pid!r} is reserved")
    where = f"phase {pid!r}"
    name = _req_str(raw, "name", where)
    done_when = _req_str(raw, "done_when", where)
    check = _opt_str(raw, "check", where)

    if "depends_on" in raw:
        deps = raw["depends_on"]
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            raise FlowError(f"{where}: depends_on must be a list of phase ids")
        if len(set(deps)) != len(deps):
            raise FlowError(f"{where}: depends_on has duplicates")
        depends_on = tuple(deps)
    else:
        depends_on = (prev_id,) if prev_id else ()

    loop = None
    if raw.get("loop") is not None:
        lr = raw["loop"]
        if not isinstance(lr, dict) or set(lr) - {"max_iterations", "until"}:
            raise FlowError(f"{where}: loop must be {{max_iterations, until}}")
        loop = Loop(
            max_iterations=_pos_int(lr.get("max_iterations"), f"{where}: loop.max_iterations"),
            until=_req_str(lr, "until", f"{where}: loop"),
        )

    on_fail = _opt_str(raw, "on_fail", where)
    for flag in ("dynamic", "side_effect"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise FlowError(f"{where}: {flag} must be a boolean")
    return Phase(
        id=pid, name=name, done_when=done_when, check=check, depends_on=depends_on,
        loop=loop, on_fail=on_fail, dynamic=bool(raw.get("dynamic", False)),
        side_effect=bool(raw.get("side_effect", False)),
    )


def _topo_order(phases: Tuple[Phase, ...]) -> List[str]:
    """Deterministic topological order (declaration order breaks ties).

    Raises FlowError naming the cycle if the dependency graph has one.
    """
    ids = [p.id for p in phases]
    deps = {p.id: list(p.depends_on) for p in phases}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {i: WHITE for i in ids}
    order: List[str] = []

    def visit(node: str, stack: List[str]) -> None:
        color[node] = GREY
        stack.append(node)
        for d in deps[node]:
            if color[d] == GREY:
                cycle = stack[stack.index(d):] + [d]
                raise FlowError("depends_on cycle: " + " -> ".join(cycle))
            if color[d] == WHITE:
                visit(d, stack)
        stack.pop()
        color[node] = BLACK
        order.append(node)

    for i in ids:
        if color[i] == WHITE:
            visit(i, [])
    return order


def parse_flow(data: Any) -> Flow:
    if not isinstance(data, dict):
        raise FlowError("flow must be a JSON object")
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise FlowError(f"unknown top-level keys {sorted(unknown)}")

    schema_version = data.get("schema_version", 1)
    if isinstance(schema_version, bool) or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise FlowError(
            f"schema_version {schema_version!r} not supported (supported: {SUPPORTED_SCHEMA_VERSIONS})"
        )
    fv = data.get("flow_version", "1")
    if isinstance(fv, bool) or not isinstance(fv, (str, int)) or str(fv).strip() == "":
        raise FlowError("flow_version must be a non-empty string or integer")

    goal = _req_str(data, "goal", "flow")
    raw_phases = data.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        raise FlowError("flow: 'phases' must be a non-empty list")

    phases: List[Phase] = []
    prev: Optional[str] = None
    for i, rp in enumerate(raw_phases):
        p = _parse_phase(rp, i, prev)
        phases.append(p)
        prev = p.id

    ids = [p.id for p in phases]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise FlowError(f"duplicate phase ids {dup}")
    known = set(ids)
    for p in phases:
        for d in p.depends_on:
            if d not in known:
                raise FlowError(f"phase {p.id!r}: depends_on unknown phase {d!r}")
            if d == p.id:
                raise FlowError(f"phase {p.id!r}: depends_on itself")
        if p.on_fail is not None:
            if p.on_fail not in known:
                raise FlowError(f"phase {p.id!r}: on_fail unknown phase {p.on_fail!r}")
            if p.on_fail == p.id:
                raise FlowError(f"phase {p.id!r}: on_fail cannot target itself")
    _topo_order(tuple(phases))
    targets = {p.on_fail for p in phases if p.on_fail}
    depended = {d for p in phases for d in p.depends_on}
    if all(p.id in targets - depended for p in phases):
        raise FlowError("every phase is a branch-only on_fail target; at least one must be a normal phase")

    privacy = dict(DEFAULT_PRIVACY)
    rpriv = data.get("privacy") or {}
    if not isinstance(rpriv, dict) or set(rpriv) - set(DEFAULT_PRIVACY):
        raise FlowError("privacy must be an object with only 'send_diff'")
    if "send_diff" in rpriv:
        if not isinstance(rpriv["send_diff"], bool):
            raise FlowError("privacy.send_diff must be a boolean")
        privacy["send_diff"] = rpriv["send_diff"]

    mode = data.get("mode", "enforce")
    if mode not in MODES:
        raise FlowError(f"mode must be one of {MODES}")
    for key in ("gates", "notify"):
        if key in data and not isinstance(data[key], dict):
            raise FlowError(f"{key} must be an object")

    return Flow(
        goal=goal, phases=tuple(phases), schema_version=schema_version,
        flow_version=str(fv).strip(), limits=_parse_limits(data.get("limits")),
        privacy=privacy, mode=mode, gates=dict(data.get("gates") or {}),
        notify=dict(data.get("notify") or {}),
    )


def load_flow(path: str) -> Flow:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise FlowError(f"flow file not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise FlowError(f"cannot read flow file {path}: {exc}") from None
    return parse_flow(data)
