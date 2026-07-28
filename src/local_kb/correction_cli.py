"""Agent-facing CLI integration for local correction memory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .correction_service import CorrectionService
from .correction_model import STATUSES, record_to_dict
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
    list_parser = subcommands.add_parser(
        "corrections-list",
        help="list bounded local correction records",
    )
    list_parser.add_argument("--vault", type=Path)
    list_parser.add_argument("--status", choices=sorted(STATUSES))
    list_parser.add_argument("--limit", type=int, default=100)
    show_parser = subcommands.add_parser(
        "corrections-show",
        help="show one correction and its timeline",
    )
    show_parser.add_argument("--vault", type=Path)
    show_parser.add_argument("--correction-id", required=True)
    status_parser = subcommands.add_parser(
        "corrections-set-status",
        help="change one correction lifecycle status",
    )
    status_parser.add_argument("--vault", type=Path)
    status_parser.add_argument("--correction-id", required=True)
    status_parser.add_argument(
        "--status",
        choices=["active", "suspended", "retired"],
        required=True,
    )
    status_parser.add_argument("--reason", required=True)
    status_parser.add_argument("--expected-hash", required=True)
    check_parser = subcommands.add_parser(
        "corrections-check",
        help="check canonical corrections and their search index",
    )
    check_parser.add_argument("--vault", type=Path)


def handle_correction_command(
    arguments: argparse.Namespace,
    paths: VaultPaths,
) -> int | None:
    if arguments.command not in {
        "correct",
        "corrections-list",
        "corrections-show",
        "corrections-set-status",
        "corrections-check",
    }:
        return None
    service = CorrectionService(paths)
    if arguments.command == "correct":
        result = service.create(
            read_json_document(arguments.packet),
            read_json_document(arguments.proposal),
        )
        report = {
            "correction_id": result.record.correction_id,
            "status": result.record.status,
            "created": result.created,
        }
    elif arguments.command == "corrections-list":
        records = service.list_records(
            status=arguments.status,
            limit=arguments.limit,
        )
        report = {
            "records": [record_to_dict(record) for record in records],
            "count": len(records),
        }
    elif arguments.command == "corrections-show":
        record = service.store.get(arguments.correction_id)
        report = {
            "record": record_to_dict(record),
            "timeline": service.store.events(record.correction_id),
        }
    elif arguments.command == "corrections-set-status":
        record = service.transition(
            arguments.correction_id,
            status=arguments.status,
            actor="user_via_agent",
            reason=arguments.reason,
            expected_hash=arguments.expected_hash,
        )
        report = {
            "correction_id": record.correction_id,
            "status": record.status,
            "content_sha256": record.content_sha256,
        }
    else:
        records, truncated = service.store.iter_records()
        healthy = (
            not truncated
            and (
                not records
                or (
                    paths.correction_index.is_file()
                    and service.index.integrity_check()
                )
            )
        )
        report = {
            "healthy": healthy,
            "record_count": len(records),
            "scan_truncated": truncated,
            "index_available": (
                not records
                or (
                    paths.correction_index.is_file()
                    and service.index.integrity_check()
                )
            ),
        }
        print(json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
        ))
        return 0 if healthy else 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0
