from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import chain
from types import MappingProxyType
from typing import Final, TextIO

from tests.integration.run import GROUPS

SUITES: Final = (*GROUPS, "management-replica", "database-replica", "browser")
SHARDS: Final = MappingProxyType({"providers": 4, "extensions": 8})
MAX_WORKERS: Final = 4


@dataclass(frozen=True, slots=True)
class Assignment:
    suite: str
    index: int
    count: int
    workers: int

    def record(self) -> dict[str, str | int]:
        return {"suite": self.suite, "index": self.index, "count": self.count, "workers": self.workers}

    def qualification_record(self) -> dict[str, str | int]:
        return {
            "suite": self.suite,
            "count": self.count,
            "artifact_pattern": f"integration-execution-{self.suite}-[0-9]*-of-{self.count}",
        }


def plan(suite: str, workers: str, focused: bool) -> tuple[Assignment, ...] | str:
    if suite != "all" and suite not in SUITES:
        return "Unknown integration group"
    if focused and suite in ("all", "browser"):
        return "Focused paths require one non-browser group"
    if workers and (not workers.isascii() or not workers.isdecimal() or not 1 <= int(workers) <= MAX_WORKERS):
        return f"Workers must be between 1 and {MAX_WORKERS} on the standard runner"
    selected: Final = SUITES if suite == "all" else (suite,)
    return tuple(chain.from_iterable(assignments(name, workers, focused) for name in selected))


def assignments(suite: str, workers: str, focused: bool) -> tuple[Assignment, ...]:
    count: Final = 1 if focused else SHARDS.get(suite, 1)
    concurrency: Final = 1 if suite == "browser" else int(workers or "2")
    return tuple(Assignment(suite, index, count, concurrency) for index in range(count))


def select_files(files: tuple[str, ...], count: int, index: int) -> tuple[str, ...] | str:
    if count < 1 or not 0 <= index < count:
        return "Shard index must be within the positive shard count"
    if not files or any(not file.strip() for file in files) or len(frozenset(files)) != len(files):
        return "Candidate filenames must be nonempty and unique"
    return tuple(sorted(files)[index::count])


def outputs(assignments_: tuple[Assignment, ...]) -> str:
    matrix: Final = {"include": tuple(assignment.record() for assignment in assignments_)}
    qualification: Final = {
        "include": tuple(
            assignment.qualification_record()
            for assignment in assignments_
            if assignment.index == 0 and assignment.suite != "browser"
        )
    }
    return f"matrix={json.dumps(matrix)}\nqualification={json.dumps(qualification)}\n"


def main(
    arguments: tuple[str, ...], environment: Mapping[str, str], source: TextIO, destination: TextIO, errors: TextIO
) -> int:
    match arguments:
        case ("matrix",):
            planned: Final = plan(
                environment.get("REQUESTED_SUITE", "all"),
                environment.get("REQUESTED_WORKERS", ""),
                bool(environment.get("INTEGRATION_TEST_NODES", "").strip()),
            )
            if isinstance(planned, str):
                errors.write(planned + "\n")
                return 1
            destination.write(outputs(planned))
            return 0
        case ("files",):
            try:
                count: Final = int(environment.get("INTEGRATION_CASE_SHARDS", "1"))
                index: Final = int(environment.get("INTEGRATION_CASE_SHARD_INDEX", "0"))
            except ValueError:
                errors.write("Shard count and index must be integers\n")
                return 1
            selected: Final = select_files(tuple(source.read().splitlines()), count, index)
            if isinstance(selected, str):
                errors.write(selected + "\n")
                return 1
            destination.write("".join(f"{file}\n" for file in selected))
            return 0
        case _:
            errors.write("Choose matrix or files\n")
            return 1


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:]), os.environ, sys.stdin, sys.stdout, sys.stderr))
