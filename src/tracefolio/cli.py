import argparse
import json
import sys

from .collection import collect_projects
from .errors import TracefolioError
from .investigation import create_packet, extend_packet
from .private_config import load_config_set, selected_config_paths
from .sources.base import ContextRequest
from .state import StateStore


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise TracefolioError("invalid_arguments", "Use --help for supported arguments.")


def parser():
    root = SafeParser(prog="tracefolio")
    root.add_argument("--version", action="version", version="tracefolio 0.1.0")
    commands = root.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config")
    config.add_subparsers(dest="config_command", required=True).add_parser("validate")
    collect = commands.add_parser("collect")
    select = collect.add_mutually_exclusive_group(required=True)
    select.add_argument("--all", action="store_true")
    select.add_argument("--project", action="append")
    packet = commands.add_parser("packet")
    create = packet.add_subparsers(dest="packet_command", required=True).add_parser("create")
    create.add_argument("--project", required=True)
    create.add_argument("--work")
    context = commands.add_parser("context")
    for option in ("packet", "source", "revision", "path"):
        context.add_argument("--" + option, required=True)
    return root


def emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def main(argv=None) -> int:
    try:
        args = parser().parse_args(argv)
        config = load_config_set(selected_config_paths())
        if args.command == "config":
            emit({"status": "valid", "configured_project_count": len(config.registry.projects),
                  "enabled_project_count": sum(p.enabled for p in config.registry.projects)})
            return 0
        if args.command == "collect":
            ids = [p.project_id for p in config.registry.projects] if args.all else args.project
            result = collect_projects(config, ids)
            emit(result.as_dict())
            return 3 if result.partial else 0
        store = StateStore.open(config)
        if args.command == "packet":
            result = create_packet(store, args.project, work_id=args.work)
        elif args.command == "context":
            packet = store.read_packet(args.packet)
            req = store.source_request(packet.project_id, args.source)
            result = extend_packet(store, ContextRequest(req, args.revision,
                                                        args.path, args.packet))
        else:
            raise TracefolioError("invalid_arguments")
        emit(result.as_dict())
        return 0 if result.status in {"created", "noop"} else 3
    except TracefolioError as error:
        print(json.dumps({"error": {"code": error.code, "message": error.safe_message}}),
              file=sys.stderr)
        return error.exit_code
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - privacy boundary
        print('{"error":{"code":"operation_failed","message":"Operation could not be completed."}}',
              file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())


