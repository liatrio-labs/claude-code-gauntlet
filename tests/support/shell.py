"""Select the shell that can execute the shipped Bash commands."""

import subprocess
import sys
from pathlib import Path


def bash_executable() -> str:
    if sys.platform != "win32":
        return "bash"
    # PATH can resolve bash to the WSL launcher even when Git Bash is installed.
    result = subprocess.run(
        ["git", "--exec-path"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    exec_path = Path(result.stdout.strip().replace("\\", "/"))
    for root in exec_path.parents:
        if any(part.casefold() == "system32" for part in root.parts):
            continue
        for relative in ("bin/bash.exe", "usr/bin/bash.exe"):
            candidate = root / relative
            if candidate.is_file():
                return str(candidate)
    raise FileNotFoundError("Git Bash was not found under the Git installation")
