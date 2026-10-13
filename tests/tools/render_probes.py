"""Record and check renderer probes."""

if __name__ == "__main__":
    import sys
    from pathlib import Path

    # The composed-case builder needs delivery and its shared test input builder.
    repo_root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repo_root / "scripts"), str(repo_root)]

    from tests.tools.probe_cli import main

    raise SystemExit(main())
