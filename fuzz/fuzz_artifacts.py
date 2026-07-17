#!/usr/bin/env python3
from __future__ import annotations

import sys

import atheris

with atheris.instrument_imports(include=["skivolve.artifacts", "rfc8785"]):
    from skivolve.artifacts import ArtifactError, normalize_artifact


@atheris.instrument_func
def test_one_input(data: bytes) -> None:
    try:
        artifact = normalize_artifact("final_output_json", data)
    except ArtifactError:
        return
    repeated = normalize_artifact("final_output_json", artifact.content)
    if repeated.content != artifact.content:
        raise AssertionError("canonical JSON normalization is not idempotent")


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
