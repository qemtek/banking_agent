from dataclasses import dataclass
from typing import Optional

SCORE_VERSION = 9


@dataclass(frozen=True)
class Check:
    criterion: str
    passed: Optional[bool]
    evidence: str
    category: str = "operation"
    expected: str = ""
    wording_signal: Optional[bool] = None
    source: str = "turn"


def communication(criterion, signal, evidence, expected=""):
    return Check(criterion, None, evidence, "communication", expected, signal)


def verdict(values):
    values = list(values)
    if not values:
        return "N/A"
    if False in values:
        return "FAIL"
    return "REVIEW" if None in values else "PASS"


def scores(checks, error=False):
    groups = {"operational": [], "communication": []}
    for check in checks:
        if check.category in ("unnecessary_work", "efficiency"):
            continue
        axis = "communication" if check.category in ("communication", "delivery") else "operational"
        groups[axis].append(check.passed)
    dimensions = {axis: ("ERROR" if error else verdict(values)) for axis, values in groups.items()}
    overall = "ERROR" if error else verdict(groups["operational"] + groups["communication"])
    if overall == "N/A":
        raise ValueError("A completed task requires at least one scored criterion")
    return overall, dimensions


def exit_code(rows):
    statuses = {row["status"] for row in rows}
    if "ERROR" in statuses:
        return 2
    if "FAIL" in statuses:
        return 1
    return 3 if "REVIEW" in statuses else 0
