"""Offline forge input for the synthetic description cases; refuse unknown calls."""

import json
import sys
from pathlib import Path

source = Path(__file__).parent
facts = json.loads((source / "input.json").read_text(encoding="utf-8"))
args = sys.argv[1:]
with (source / "forge-calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\n")
if args[:2] == ["pr", "view"] and "--json" in args:
    fields = args[args.index("--json") + 1].split(",")
    print(json.dumps({key: facts[key] for key in fields}))
elif args[:2] == ["pr", "diff"]:
    print((source / "diff.patch").read_text(encoding="utf-8"), end="")
elif args[:2] == ["repo", "view"]:
    print(
        json.dumps(
            {
                "defaultBranchRef": {"name": "main"},
                "nameWithOwner": "synthetic/collection",
            }
        )
    )
elif args and args[0] == "api":
    endpoint = next(
        (arg for arg in args[1:] if arg == "user" or arg.startswith("repos/")), ""
    )
    if endpoint == "user":
        print('{"login":"synthetic"}')
    elif "/files" in endpoint:
        print(json.dumps("collection.py"))
    elif endpoint.endswith(("/reviews", "/comments")):
        print("[]")
    elif endpoint.endswith("/pulls/1"):
        print(
            json.dumps(
                {
                    "head": {"sha": facts["headRefOid"]},
                    "base": {"sha": facts["baseRefOid"]},
                }
            )
        )
    else:
        sys.exit("unsupported synthetic forge API")
else:
    sys.exit("unsupported synthetic forge call")
