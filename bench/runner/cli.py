"""Common explicit spec/report command boundary."""

import argparse
from pathlib import Path


def spec_output_args(description, argv):
    parser = argparse.ArgumentParser(description=description)
    for key in ("spec", "out"):
        parser.add_argument("--" + key, type=Path, required=True)
    return parser.parse_args(argv)
