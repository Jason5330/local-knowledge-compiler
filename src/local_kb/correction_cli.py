"""Agent-facing CLI integration for local correction memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .correction_service import CorrectionService
from .finalize import read_json_document
from .paths import VaultPaths


def add_correction_parsers(
    subcommands: argparse._SubParsersAction,
) -> None:
    parser = subcommands.add_parser(
        "correct",
        help="create one evidence-grounded correction",
    )
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)


def handle_correction_command(
    arguments: argparse.Namespace,
    paths: VaultPaths,
) -> int | None:
    if arguments.command != "correct":
        return None
    result = CorrectionService(paths).create(
        read_json_document(arguments.packet),
        read_json_document(arguments.proposal),
    )
    print(json.dumps(
        {
            "correction_id": result.record.correction_id,
            "status": result.record.status,
            "created": result.created,
        },
        ensure_ascii=False,
        sort_keys=True,
    ))
    return 0
