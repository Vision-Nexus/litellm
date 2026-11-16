from __future__ import annotations

from collections import Counter
from fnmatch import fnmatchcase
from io import StringIO
from itertools import chain
from typing import Final

import pytest
from pydantic import TypeAdapter

from tests.integration._support.github_shards import MAX_WORKERS, SHARDS, SUITES, main, plan, select_files
from tests.integration._support.sharding import ShardExecution, shard_errors

MATRIX: Final = TypeAdapter(dict[str, tuple[dict[str, str | int], ...]])


@pytest.mark.parametrize("suite", ("all", *SUITES))
def test_every_requested_group_has_all_shards_and_qualifies_once(suite: str) -> None:
    planned: Final = plan(suite, "", False)
    assert not isinstance(planned, str)
    expected: Final = frozenset(SUITES if suite == "all" else (suite,))
    assert frozenset(assignment.suite for assignment in planned) == expected
    for name in expected:
        group: Final = tuple(assignment for assignment in planned if assignment.suite == name)
        assert tuple(assignment.index for assignment in group) == tuple(range(SHARDS.get(name, 1)))
        assert all(assignment.count == len(group) for assignment in group)


def test_explicit_worker_count_reaches_every_non_browser_assignment() -> None:
    planned: Final = plan("all", str(MAX_WORKERS), False)
    assert not isinstance(planned, str)
    assert all(assignment.workers == (1 if assignment.suite == "browser" else MAX_WORKERS) for assignment in planned)


@pytest.mark.parametrize("suite", tuple(SHARDS))
def test_focused_paths_run_once_instead_of_repeating_on_each_full_suite_shard(suite: str) -> None:
    planned: Final = plan(suite, "1", True)
    assert not isinstance(planned, str)
    assert len(planned) == 1
    assert planned[0].index == 0
    assert planned[0].count == 1
    assert planned[0].workers == 1


@pytest.mark.parametrize("workers", ("0", "-1", "auto", "2; false", "2.5", "２", str(MAX_WORKERS + 1)))
def test_invalid_worker_input_is_rejected_before_provisioning_runners(workers: str) -> None:
    assert isinstance(plan("providers", workers, False), str)


@pytest.mark.parametrize("suite,focused", (("missing", False), ("all", True), ("browser", True)))
def test_invalid_selections_are_rejected(suite: str, focused: bool) -> None:
    assert isinstance(plan(suite, "", focused), str)


@pytest.mark.parametrize("count", (1, 4, 8, 12))
def test_file_assignments_are_disjoint_complete_and_independent_of_input_order(count: int) -> None:
    files: Final = tuple(f"tests/integration/providers/test_case_{index}.py" for index in range(9))
    selected: Final = tuple(select_files(files, count, index) for index in range(count))
    assert all(not isinstance(shard, str) for shard in selected)
    assert Counter(chain.from_iterable(selected)) == Counter(files)
    assert selected == tuple(select_files(tuple(reversed(files)), count, index) for index in range(count))


def test_assignments_qualify_with_the_existing_census_and_reject_lost_or_duplicate_shards() -> None:
    files: Final = ("test_a.py", "test_b.py", "test_c.py")
    inventory: Final = tuple(f"{file}::test_one" for file in files)
    executions: Final = tuple(
        ShardExecution(
            inventory=inventory,
            collected=tuple(f"{file}::test_one" for file in select_files(files, 4, index)),
            passed=tuple(f"{file}::test_one" for file in select_files(files, 4, index)),
            skipped=(),
            shard_count=4,
            shard_index=index,
            complete=True,
            exitstatus=0,
        )
        for index in range(4)
    )
    assert shard_errors(executions, 4) == ()
    assert shard_errors(executions[:-1], 4)
    assert shard_errors((*executions, executions[0]), 4)
    assert shard_errors((*executions[:-1], executions[-1].model_copy(update={"complete": False})), 4)


@pytest.mark.parametrize(
    "files,count,index",
    (((), 2, 0), (("a", "a"), 2, 0), (("",), 2, 0), (("a",), 0, 0), (("a",), 2, 2)),
)
def test_bad_file_inventory_or_shard_index_cannot_fall_back_to_the_full_suite(
    files: tuple[str, ...], count: int, index: int
) -> None:
    assert isinstance(select_files(files, count, index), str)


def test_empty_assignment_emits_no_filenames_and_a_missing_inventory_fails() -> None:
    output: Final = StringIO()
    errors: Final = StringIO()
    environment: Final = {"INTEGRATION_CASE_SHARDS": "4", "INTEGRATION_CASE_SHARD_INDEX": "3"}
    assert main(("files",), environment, StringIO("test_a.py\n"), output, errors) == 0
    assert output.getvalue() == ""
    assert errors.getvalue() == ""
    assert main(("files",), environment, StringIO(), output, errors) == 1
    assert errors.getvalue()


def test_matrix_command_outputs_separate_execution_and_qualification_plans() -> None:
    output: Final = StringIO()
    errors: Final = StringIO()
    assert main(("matrix",), {"REQUESTED_SUITE": "providers"}, StringIO(), output, errors) == 0
    values: Final = dict(line.split("=", 1) for line in output.getvalue().splitlines())
    assert len(MATRIX.validate_json(values["matrix"])["include"]) == SHARDS["providers"]
    assert MATRIX.validate_json(values["qualification"]) == {
        "include": (
            {
                "suite": "providers",
                "count": SHARDS["providers"],
                "artifact_pattern": f"integration-execution-providers-[0-9]*-of-{SHARDS['providers']}",
            },
        )
    }
    assert errors.getvalue() == ""


def test_qualification_downloads_only_its_group_and_never_the_replica_sibling() -> None:
    output: Final = StringIO()
    assert main(("matrix",), {"REQUESTED_SUITE": "all"}, StringIO(), output, StringIO()) == 0
    values: Final = dict(line.split("=", 1) for line in output.getvalue().splitlines())
    planned: Final = MATRIX.validate_json(values["matrix"])["include"]
    groups: Final = MATRIX.validate_json(values["qualification"])["include"]
    names: Final = {
        f"integration-execution-{item['suite']}-{item['index']}-of-{item['count']}": item["suite"] for item in planned
    }
    for group in groups:
        pattern: Final = group["artifact_pattern"]
        assert isinstance(pattern, str)
        assert frozenset(name for name in names if fnmatchcase(name, pattern)) == frozenset(
            name for name, suite in names.items() if suite == group["suite"]
        )
