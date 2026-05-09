"""Top-level CLI for media-toolkit.

Subcommands are discovered per domain (videos / photos / pdfs). Each domain
package exposes an ``OPS`` list of op modules. Each op module is expected to
provide ``register_subparser(subparsers)`` and ``run(args)``. When invoked
with no (or partial) args, the CLI drops into a questionary-driven menu to
fill in the missing pieces.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from media_toolkit import photos, pdfs, videos
from media_toolkit.logging_setup import configure_logging

logger = logging.getLogger(__name__)

DOMAINS: dict[str, Any] = {
    "videos": videos,
    "photos": photos,
    "pdfs": pdfs,
}

# Default log file path. Override via `--log-file PATH`.
DEFAULT_LOG_FILE = Path("/tmp/media-toolkit.log")

# Sentinel labels used in the interactive menu.
MENU_QUIT_LABEL = "Quit"
DOMAIN_MENU_CHOICES: list[tuple[str, str]] = [
    ("Videos", "videos"),
    ("Photos", "photos"),
    ("PDFs", "pdfs"),
]

# Exit codes (mirrors HANDOFF Section 7).
# - 0 success / 1 generic-failure / 2 setup-error
# - 130 = standard SIGINT (Ctrl-C) convention.
EXIT_UNEXPECTED_ERROR = 1
EXIT_USER_ABORT = 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-toolkit",
        description="Personal media-processing toolkit (videos / photos / pdfs).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help=(
            f"Path to the log file (default: {DEFAULT_LOG_FILE}). "
            "Everything also goes to stdout unless --quiet is set on the op."
        ),
    )
    domain_subparsers = parser.add_subparsers(dest="domain")

    for domain_name, domain_module in DOMAINS.items():
        domain_parser = domain_subparsers.add_parser(
            domain_name, help=f"{domain_name} operations"
        )
        op_subparsers = domain_parser.add_subparsers(dest="op")
        for op_module in getattr(domain_module, "OPS", []):
            op_module.register_subparser(op_subparsers)

    return parser


def _prompt_domain() -> str | None:
    """Prompt for a domain. Returns the domain key or None on abort/quit."""
    import questionary

    choices = [label for label, _ in DOMAIN_MENU_CHOICES] + [MENU_QUIT_LABEL]
    answer = questionary.select("What type of media?", choices=choices).ask()
    if answer is None or answer == MENU_QUIT_LABEL:
        return None
    for label, key in DOMAIN_MENU_CHOICES:
        if answer == label:
            return key
    return None


def _prompt_op(domain_module: Any) -> Any | None:
    """Prompt for an op within the given domain. Returns the op module or None."""
    import questionary

    ops = getattr(domain_module, "OPS", [])
    if not ops:
        print("No operations available yet for this domain.")
        return None

    label_to_op = {f"{op.NAME} - {op.DESCRIPTION}": op for op in ops}
    choices = list(label_to_op.keys()) + [MENU_QUIT_LABEL]
    answer = questionary.select(
        "Choose an operation:", choices=choices
    ).ask()
    if answer is None or answer == MENU_QUIT_LABEL:
        return None
    return label_to_op.get(answer)


def _resolve_op_module(domain: str, op_name: str | None) -> Any | None:
    """Look up an op module by domain key + op NAME. Returns None if not found."""
    domain_module = DOMAINS.get(domain)
    if domain_module is None:
        return None
    for op_module in getattr(domain_module, "OPS", []):
        if op_module.NAME == op_name:
            return op_module
    return None


def main(argv: list[str] | None = None) -> int:
    """Entry point for the media-toolkit CLI."""
    args = sys.argv[1:] if argv is None else argv

    parser = _build_parser()
    parsed = parser.parse_args(args)

    # Configure logging early so every subsequent message goes through it.
    # `--quiet` is a per-op flag; suppress console below WARNING when set.
    quiet = bool(getattr(parsed, "quiet", False))
    console_level = logging.WARNING if quiet else logging.INFO
    configure_logging(parsed.log_file, console_level=console_level)

    # Step 1: resolve domain (interactively if missing).
    domain = parsed.domain
    if domain is None:
        domain = _prompt_domain()
        if domain is None:
            return EXIT_USER_ABORT
        parsed.domain = domain

    domain_module = DOMAINS.get(domain)
    if domain_module is None:
        parser.print_help()
        return 0

    # Step 2: resolve op (interactively if missing).
    op_module = _resolve_op_module(domain, getattr(parsed, "op", None))
    if op_module is None:
        # Empty domains (photos/pdfs today) exit cleanly with a notice.
        if not getattr(domain_module, "OPS", []):
            print(f"No operations available yet for '{domain}'.")
            return 0
        op_module = _prompt_op(domain_module)
        if op_module is None:
            return EXIT_USER_ABORT
        parsed.op = op_module.NAME

    # Step 3: dispatch. The op's run() will call interactive_args(prefilled)
    # internally to fill any remaining missing fields.
    #
    # Wrap the dispatch in a broad except so an unexpected exception inside an
    # op surfaces as a clean exit-code-1 with a logged error rather than a raw
    # Python traceback. Argparse / --help errors happen earlier (in
    # ``parser.parse_args``) and keep their normal behavior.
    try:
        return int(op_module.run(parsed) or 0)
    except KeyboardInterrupt:
        logger.warning("aborted by user (KeyboardInterrupt)")
        return EXIT_USER_ABORT
    except Exception:
        logger.exception("unexpected error during op dispatch")
        return EXIT_UNEXPECTED_ERROR


if __name__ == "__main__":
    sys.exit(main())
