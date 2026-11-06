"""Shared argument checks for explicit local Compose fault-test targets."""
import argparse
import re
from urllib.parse import urlparse

DEFAULT_PROJECT = "data-cleaning-pipeline"
DEFAULT_BASE_URL = "http://localhost:8088"


def local_base_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        port = parsed.port
        valid = (parsed.scheme in {"http", "https"}
                 and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment
                 and parsed.path in {"", "/"}
                 and (port is None or 1 <= port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("Fault tests require an HTTP(S) loopback base URL without credentials/path/query")
    return value.rstrip("/")


def local_project(value: str) -> str:
    if not re.fullmatch(r"data-cleaning-pipeline(?:-[a-z0-9][a-z0-9_-]*)?", value):
        raise argparse.ArgumentTypeError("Project must be data-cleaning-pipeline or data-cleaning-pipeline-<local-suffix>")
    return value


def add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", type=local_base_url, default=DEFAULT_BASE_URL,
                        help="local Java/browser origin; default %(default)s")
    parser.add_argument("--project", type=local_project, default=DEFAULT_PROJECT,
                        help="explicit docker compose -p project; default %(default)s")


def compose_command(project: str, *arguments: str) -> list[str]:
    return ["docker", "compose", "-p", local_project(project), *arguments]
