"""Measure local installed files; this is not a PythonAnywhere deployment audit."""
import json
import os
from pathlib import Path
import sys


def disk_files(path):
    total = 0
    count = 0
    for directory, _, files in os.walk(path, followlinks=False):
        for filename in files:
            file = Path(directory) / filename
            if file.is_symlink():
                continue
            total += file.stat().st_size
            count += 1
    return {"path": str(path.resolve()), "file_bytes": total, "files": count}


workspace = Path(__file__).resolve().parent.parent
parts = {
    "backend": disk_files(workspace / "backend"),
    "virtual_environment": disk_files(Path(sys.prefix)),
    "python_base_installation": disk_files(Path(sys.base_prefix)),
}
report = {
    "measurement": "local Windows reference, logical file lengths; not allocated disk blocks",
    "python": sys.version.split()[0],
    "parts": parts,
    "total_file_bytes": sum(part["file_bytes"] for part in parts.values()),
    "initial_budget_bytes": 500_000_000,
    "limitations": [
        "Entire existing Python base installation included, including unrelated global packages.",
        "No production media, initial business dataset, backup staging or future dependencies yet.",
        "PythonAnywhere Linux runtime, quota and allocated blocks must be measured separately.",
    ],
}
output = workspace / "analysis" / "backend-local-size.json"
output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
