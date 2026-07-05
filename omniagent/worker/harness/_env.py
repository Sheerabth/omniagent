"""Parse a dotenv file into a dict — no dependencies, no side effects."""

import os


def _load_env_file(path: str) -> dict[str, str]:
    """Read *path* (KEY=VALUE lines), return a dict.  Skips blank lines and
    comments, strips quotes.  Returns {} if the file does not exist."""
    if not os.path.isfile(path):
        return {}
    result: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("\"'")
            result[k] = v
    return result
