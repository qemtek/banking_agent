import hashlib
import json
from pathlib import Path


def trial_directory(batch, row):
    relative = Path(row.get("trial_dir", "."))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Trial directory must be inside the batch")
    return batch / relative


def digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_contents(book):
    if not book["cases"]:
        raise ValueError("Empty casebook")
    seen = set()
    for entry in book["cases"]:
        case = dict(entry)
        case_id = case.pop("case_id")
        if case_id in seen or digest([book["version"], case]) != case_id or case["split"] != book["split"]:
            raise ValueError("Duplicate, mismatched or modified case")
        seen.add(case_id)
