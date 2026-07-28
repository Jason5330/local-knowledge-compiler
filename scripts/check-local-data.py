"""Block accidental Git publication of project-local user data."""

import argparse
import subprocess
import sys


PREFIX = "knowledgebase/"


def protected(paths: list[str]) -> list[str]:
    return [
        path
        for path in paths
        if path.replace("\\", "/")
        .lstrip("./")
        .casefold()
        .startswith(PREFIX)
    ]


def git_paths(mode: str) -> list[str]:
    if mode == "staged":
        command = (
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "-z",
        )
    else:
        command = ("git", "ls-files", "-z")
    completed = subprocess.run(
        command,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Could not inspect Git paths; publication blocked."
        )
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in completed.stdout.split(b"\0")
        if item
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=("staged", "tracked"),
    )
    arguments = parser.parse_args(argv)
    try:
        paths = git_paths(arguments.mode)
    except RuntimeError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    violations = protected(paths)
    if not violations:
        return 0
    sys.stderr.write(
        "Blocked: local KnowledgeBase data must never be published.\n"
    )
    for path in violations:
        sys.stderr.write(f"- {path}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
