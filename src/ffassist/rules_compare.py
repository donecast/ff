"""Diff two MFL rules payloads with proper aggregation.

MFL rules are organized as multiple `positionRules` entries. Each entry applies its
rule list to one or more positions (pipe-separated). When the SAME (event, range)
appears in multiple entries that both cover the same position, the values STACK
additively — e.g. an "any skill position" entry with `RA *0.25` plus a TE-specific
entry with `RA *3` means TE rush attempts are worth 3.25 each, not 3.

Earlier versions of this module overwrote instead of summing, which produced
false differences between leagues that have the same effective scoring but
different rule-text organization.
"""

from __future__ import annotations

import re


def _unwrap(v):
    """MFL XML->JSON leaves text in {'$t': '...'} wrappers — unwrap recursively."""
    if isinstance(v, dict):
        if set(v.keys()) == {"$t"}:
            return v["$t"]
        return {k: _unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


def _split_positions(s: str) -> list[str]:
    return [p.strip() for p in re.split(r"[|,]", s or "") if p.strip()]


def _normalize_value(s: str) -> float | None:
    """Parse MFL value like '*0.5', '.1/10', '*-3', '15' to a numeric multiplier or flat."""
    s = (s or "").strip()
    if not s:
        return None
    if "/" in s:
        try:
            num, den = s.split("/", 1)
            return float(num.lstrip("*")) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    s = s.lstrip("*")
    try:
        return float(s)
    except ValueError:
        return None


def _index_position_rules(rules: dict) -> dict[str, dict[str, float]]:
    """Return {position: {event[range]: aggregated_value}} with additive stacking."""
    rules = _unwrap(rules)
    out: dict[str, dict[str, float]] = {}
    pr = rules.get("positionRules", [])
    if isinstance(pr, dict):
        pr = [pr]
    for entry in pr:
        positions = entry.get("positions", "")
        rule_list = entry.get("rule", [])
        if isinstance(rule_list, dict):
            rule_list = [rule_list]
        for r in rule_list:
            ev = r.get("event") or r.get("abbreviation") or "?"
            pts = r.get("points", "0")
            rng = r.get("range", "")
            key = f"{ev} [{rng}]" if rng else ev
            val = _normalize_value(str(pts))
            if val is None:
                continue
            for pos in _split_positions(positions):
                pos_rules = out.setdefault(pos, {})
                pos_rules[key] = round(pos_rules.get(key, 0.0) + val, 6)
    return out


def _fmt(v: float) -> str:
    if v == int(v):
        return f"{int(v)}"
    return f"{v:g}"


def diff_position_rules(a: dict, b: dict) -> dict[str, list[str]]:
    aidx = _index_position_rules(a)
    bidx = _index_position_rules(b)
    all_positions = sorted(set(aidx) | set(bidx))
    result: dict[str, list[str]] = {}
    for pos in all_positions:
        lines: list[str] = []
        ar = aidx.get(pos, {})
        br = bidx.get(pos, {})
        for key in sorted(set(ar) | set(br)):
            va = ar.get(key)
            vb = br.get(key)
            if va is None and vb is None:
                continue
            # Treat near-zero diffs as identical
            if va is not None and vb is not None and abs(va - vb) < 1e-6:
                continue
            if va is None:
                lines.append(f"  + {key}: {_fmt(vb)}")
            elif vb is None:
                lines.append(f"  - {key}: {_fmt(va)}")
            else:
                lines.append(f"  ~ {key}: {_fmt(va)} -> {_fmt(vb)}")
        if lines:
            result[pos] = lines
    return result


def format_diff(label_a: str, label_b: str, diff: dict[str, list[str]]) -> str:
    if not diff:
        return f"No scoring differences between {label_a} and {label_b}."
    out = [f"Scoring differences: {label_a} -> {label_b}", "  (- removed in B, + new in B, ~ changed)"]
    for pos, lines in diff.items():
        out.append(f"\n[{pos}]")
        out.extend(lines)
    return "\n".join(out)
