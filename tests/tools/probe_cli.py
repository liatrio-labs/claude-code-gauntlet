from __future__ import annotations

import argparse

from tests.tools.probe_fixtures import (
    _fixture_path,
    _quick_action_fixture_path,
    _read_cases,
    record_divergence,
    record_fixture,
)
from tests.tools.probe_github import (
    github_login,
    render_github,
)
from tests.tools.probe_gitlab import (
    _client_from_environment,
    _gitlab_blob_prefix,
    _gitlab_version,
    seed,
)
from tests.tools.probe_quick_actions import (
    QUICK_ACTION_CONTAINER,
    record_quick_action_fixture,
)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record Markdown renderer probes.")
    commands = parser.add_subparsers(dest="command", required=True)
    seed_parser = commands.add_parser(
        "seed", help="seed the local GitLab render project"
    )
    seed_parser.add_argument("--base-url", default="http://localhost:8929")
    seed_parser.add_argument("--token-env", default="GITLAB_TOKEN")
    record_parser = commands.add_parser(
        "record", help="record or check a renderer probe"
    )
    record_parser.add_argument(
        "--platform", choices=("github", "gitlab"), required=True
    )
    record_parser.add_argument("--check", action="store_true")
    record_parser.add_argument("--base-url", default="http://localhost:8929")
    record_parser.add_argument("--token-env", default="GITLAB_TOKEN")
    record_parser.add_argument("--project", default="cdr-group/probe")
    quick_actions_parser = commands.add_parser(
        "quick-actions", help="record or check GitLab quick-action verdicts"
    )
    quick_actions_parser.add_argument(
        "action", choices=("record",), nargs="?", default="record"
    )
    quick_actions_parser.add_argument("--check", action="store_true")
    quick_actions_parser.add_argument("--container", default=QUICK_ACTION_CONTAINER)
    divergence_parser = commands.add_parser(
        "divergence", help="bind a text-only render difference"
    )
    divergence_parser.add_argument("--id", required=True)
    divergence_parser.add_argument("--issue", required=True, type=int)
    divergence_parser.add_argument("--note", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    path = _fixture_path()
    if args.command == "divergence":
        record_divergence(path, case_id=args.id, issue=args.issue, note=args.note)
        return 0
    if args.command == "seed":
        client = _client_from_environment(args.base_url, args.token_env)
        seeded = seed(_read_cases(path), client)
        print(f"seeded {len(seeded['users'])} users and {len(seeded['groups'])} groups")
        return 0
    if args.command == "quick-actions":
        quick_result = record_quick_action_fixture(
            _quick_action_fixture_path(),
            _read_cases(path),
            container=args.container,
            check=args.check,
        )
        if args.check:
            if quick_result.changed_ids:
                for case_id in quick_result.changed_ids:
                    print(case_id)
            else:
                print("GitLab quick-action verdict fixture matches")
        else:
            print(
                f"recorded {len(quick_result.document['cases'])} GitLab quick-action verdicts"
            )
        return quick_result.exit_code
    if args.platform == "github":
        result = record_fixture(
            path,
            platform="github",
            render=render_github,
            check=args.check,
            get_github_login=github_login,
        )
    else:
        client = _client_from_environment(args.base_url, args.token_env)

        def render(text: str) -> str:
            return client.render(text, args.project)

        version = _gitlab_version(client)
        blob_prefix = _gitlab_blob_prefix(client, args.project)
        result = record_fixture(
            path,
            platform="gitlab",
            render=render,
            version=version,
            blob_prefix=blob_prefix,
            check=args.check,
        )
    for case_id in result.changed_ids:
        print(case_id)
    for detail in result.cleared_details:
        print(f"cleared divergence: {detail}")
    if args.check:
        for case_id in result.divergence_cleared:
            print(f"stale divergence: {case_id}")
    if result.version_mismatch:
        before, after = result.version_mismatch
        print(f"version mismatch: {before} -> {after}")
    return result.exit_code
