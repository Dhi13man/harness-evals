#!/usr/bin/env python3
"""External differential and workload-scoped relative-performance oracle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import sys

sys.path.insert(0, os.environ["EVAL_SHARED_ROOT"])
from go_external_oracle import (  # noqa: E402
    GoModulePolicy,
    GoOracleError,
    build_external_go_oracle,
    run_go_mode,
)


def result(identifier: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": identifier, "passed": passed, "evidence": evidence}


def strict_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


workspace = Path(os.environ["EVAL_WORKSPACE"]).resolve()
policy = GoModulePolicy(
    module_path="example.com/tagrank",
    package_name="tagrank",
    required_source="ranking.go",
    api_contract="tagrank-v1",
)
harness_source = r"""package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"reflect"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"time"

	candidate "example.com/tagrank"
)

const protocol = "external-go-oracle-v1"

type envelope struct {
	Protocol     string `json:"protocol"`
	Token        string `json:"token"`
	Mode         string `json:"mode"`
	Complete     bool   `json:"complete"`
	Observations any    `json:"observations"`
}

type oracleWorkload struct {
	name   string
	values []string
	limit  int
}

type functionalObservations struct {
	CasesChecked   int `json:"cases_checked"`
	InputMutations int `json:"input_mutations"`
	Mismatches     int `json:"mismatches"`
}

type timingRecord struct {
	Name      string `json:"name"`
	N         int    `json:"n"`
	Round     int    `json:"round"`
	Candidate int64  `json:"candidate_ns"`
	Baseline  int64  `json:"baseline_ns"`
	Mismatches int    `json:"mismatches"`
}

var resultSink []candidate.Entry

func oracleReference(values []string, limit int) []candidate.Entry {
	if limit <= 0 {
		return []candidate.Entry{}
	}
	first := make([]string, 0)
	counts := make(map[string]int)
	for _, value := range values {
		if _, exists := counts[value]; !exists {
			first = append(first, value)
		}
		counts[value]++
	}
	entries := make([]candidate.Entry, 0, len(first))
	for _, value := range first {
		entries = append(entries, candidate.Entry{Value: value, Count: counts[value]})
	}
	sort.SliceStable(entries, func(i, j int) bool { return entries[i].Count > entries[j].Count })
	if limit < len(entries) {
		entries = entries[:limit]
	}
	return entries
}

func oracleQuadratic(values []string, limit int) []candidate.Entry {
	if limit <= 0 {
		return []candidate.Entry{}
	}
	counts := make([]candidate.Entry, 0)
	for _, value := range values {
		found := false
		for index := range counts {
			if counts[index].Value == value {
				counts[index].Count++
				found = true
				break
			}
		}
		if !found {
			counts = append(counts, candidate.Entry{Value: value, Count: 1})
		}
	}
	sort.SliceStable(counts, func(i, j int) bool { return counts[i].Count > counts[j].Count })
	if limit < len(counts) {
		counts = counts[:limit]
	}
	return counts
}

func oracleWorkloads() []oracleWorkload {
	unique := make([]string, 0, 9000)
	for index := 0; index < 9000; index++ {
		unique = append(unique, fmt.Sprintf("unique-%05d", index))
	}

	uniform := make([]string, 0, 12000)
	for index := 0; index < 12000; index++ {
		uniform = append(uniform, fmt.Sprintf("uniform-%04d", index%1200))
	}

	skewed := make([]string, 0, 12000)
	for index := 0; index < 2500; index++ {
		skewed = append(skewed, fmt.Sprintf("skew-%04d", index))
	}
	for index := len(skewed); index < 12000; index++ {
		skewed = append(skewed, fmt.Sprintf("skew-%04d", index%20))
	}

	return []oracleWorkload{
		{name: "unique-heavy", values: unique, limit: 200},
		{name: "uniform-duplicates", values: uniform, limit: 200},
		{name: "skewed-duplicates", values: skewed, limit: 200},
	}
}

func observeFunctional() functionalObservations {
	cases := []struct {
		values []string
		limit  int
	}{
		{[]string{"b", "a", "b", "c", "a", "b"}, 3},
		{[]string{"first", "second", "third"}, 2},
		{[]string{"x", "x", "y", "y", "z", "z"}, 10},
		{nil, 4},
		{[]string{"x"}, 0},
		{[]string{"x"}, -2},
	}
	for _, workload := range oracleWorkloads() {
		cases = append(cases, struct {
			values []string
			limit  int
		}{workload.values, workload.limit})
	}
	observations := functionalObservations{}
	for _, test := range cases {
		before := append([]string(nil), test.values...)
		got := candidate.MostFrequent(test.values, test.limit)
		want := oracleReference(test.values, test.limit)
		observations.CasesChecked++
		if !reflect.DeepEqual(got, want) {
			observations.Mismatches++
		}
		if !reflect.DeepEqual(test.values, before) {
			observations.InputMutations++
		}
	}
	return observations
}

func observePerformance(workloadIndex, round int) timingRecord {
	workload := oracleWorkloads()[workloadIndex]
	candidateInput := append([]string(nil), workload.values...)
	baselineInput := append([]string(nil), workload.values...)
	var candidateResult []candidate.Entry
	measure := func(run func()) int64 {
		runtime.GC()
		started := time.Now()
		run()
		return time.Since(started).Nanoseconds()
	}
	candidateRun := func() {
		candidateResult = candidate.MostFrequent(candidateInput, workload.limit)
		resultSink = candidateResult
	}
	baselineRun := func() {
		resultSink = oracleQuadratic(baselineInput, workload.limit)
	}
	var candidateDuration, baselineDuration int64
	if round%2 == 0 {
		candidateDuration = measure(candidateRun)
		baselineDuration = measure(baselineRun)
	} else {
		baselineDuration = measure(baselineRun)
		candidateDuration = measure(candidateRun)
	}
	mismatches := 0
	if !reflect.DeepEqual(candidateResult, oracleReference(workload.values, workload.limit)) {
		mismatches++
	}
	return timingRecord{
		Name: workload.name, N: len(workload.values), Round: round,
		Candidate: candidateDuration, Baseline: baselineDuration, Mismatches: mismatches,
	}
}

func main() {
	mode := flag.String("mode", "", "oracle mode")
	token := flag.String("token", "", "parent completion token")
	flag.Parse()
	if *mode == "" || *token == "" || flag.NArg() != 0 {
		os.Exit(2)
	}
	var observations any
	switch *mode {
	case "functional":
		observations = observeFunctional()
	default:
		parts := strings.Split(*mode, "-")
		if len(parts) != 3 || parts[0] != "performance" {
			os.Exit(2)
		}
		workloadIndex, firstError := strconv.Atoi(parts[1])
		round, secondError := strconv.Atoi(parts[2])
		if firstError != nil || secondError != nil || workloadIndex < 0 || workloadIndex >= len(oracleWorkloads()) || round < 0 || round >= 7 {
			os.Exit(2)
		}
		observations = observePerformance(workloadIndex, round)
	}
	if err := json.NewEncoder(os.Stdout).Encode(envelope{
		Protocol: protocol, Token: *token, Mode: *mode, Complete: true,
		Observations: observations,
	}); err != nil {
		os.Exit(3)
	}
}
"""

metrics: dict[str, object] = {
    "scope": {
        "host": platform.platform(),
        "claim": "interleaved median ratios for the declared hidden workloads on this host only",
    },
    "workloads": {},
}
BUILD_TIMEOUT_SECONDS = 90
MODE_TIMEOUT_SECONDS = 12
PERFORMANCE_WORKLOADS = 3
PERFORMANCE_ROUNDS = 7
mode_results: dict[str, dict[str, object]] = {}
performance_records: list[dict[str, object]] = []
mode_errors: dict[str, str] = {}
try:
    with build_external_go_oracle(
        workspace,
        policy,
        harness_source,
        race=False,
        timeout_seconds=BUILD_TIMEOUT_SECONDS,
    ) as built:
        metrics["scope"]["go_version"] = built.go_version  # type: ignore[index]
        try:
            mode_results["functional"] = run_go_mode(
                built, "functional", MODE_TIMEOUT_SECONDS
            ).observations
        except GoOracleError as error:
            mode_errors["functional"] = str(error)
        for workload_index in range(PERFORMANCE_WORKLOADS):
            for round_index in range(PERFORMANCE_ROUNDS):
                mode = f"performance-{workload_index}-{round_index}"
                try:
                    performance_records.append(
                        run_go_mode(built, mode, MODE_TIMEOUT_SECONDS).observations
                    )
                except GoOracleError as error:
                    mode_errors["performance"] = f"{mode}: {error}"
                    break
except (GoOracleError, OSError, ValueError) as error:
    detail = f"external Go harness failed: {type(error).__name__}: {error}"
    mode_errors = {mode: detail for mode in ("functional", "performance")}

functional = mode_results.get("functional", {})
functional_ok = (
    set(functional) == {"cases_checked", "input_mutations", "mismatches"}
    and strict_integer(functional.get("cases_checked")) == 9
    and strict_integer(functional.get("input_mutations")) == 0
    and strict_integer(functional.get("mismatches")) == 0
)
functional_evidence = (
    "nine differential distributions passed with only standard-library dependencies"
    if functional_ok
    else mode_errors.get("functional", f"observations={functional!r}")
)

expected_workloads = (
    ("unique-heavy", 9000),
    ("uniform-duplicates", 12000),
    ("skewed-duplicates", 12000),
)
timings: dict[str, dict[str, float | int]] = {}
performance_ok = (
    "performance" not in mode_errors
    and len(performance_records) == len(expected_workloads) * 7
)
if performance_ok:
    for workload_index, (expected_name, expected_size) in enumerate(expected_workloads):
        records = performance_records[workload_index * 7 : (workload_index + 1) * 7]
        candidates: list[int] = []
        baselines: list[int] = []
        for round_index, record in enumerate(records):
            if not isinstance(record, dict) or set(record) != {
                "baseline_ns",
                "candidate_ns",
                "mismatches",
                "n",
                "name",
                "round",
            }:
                performance_ok = False
                break
            candidate_ns = strict_integer(record.get("candidate_ns"))
            baseline_ns = strict_integer(record.get("baseline_ns"))
            if (
                record.get("name") != expected_name
                or strict_integer(record.get("n")) != expected_size
                or strict_integer(record.get("round")) != round_index
                or strict_integer(record.get("mismatches")) != 0
                or candidate_ns is None
                or baseline_ns is None
                or candidate_ns <= 0
                or baseline_ns <= 0
            ):
                performance_ok = False
                break
            candidates.append(candidate_ns)
            baselines.append(baseline_ns)
        if not performance_ok:
            break
        candidate_ns = sorted(candidates)[3]
        baseline_ns = sorted(baselines)[3]
        if candidate_ns * 2 >= baseline_ns:
            performance_ok = False
            break
        timings[expected_name] = {
            "n": expected_size,
            "candidate_median_ns": candidate_ns,
            "quadratic_baseline_median_ns": baseline_ns,
            "relative_speedup": baseline_ns / candidate_ns,
        }
metrics["workloads"] = timings
performance_evidence = (
    "workload-scoped relative medians on this host: "
    + ", ".join(
        f"{name}={data['relative_speedup']:.2f}x"
        for name, data in sorted(timings.items())
    )
    + "; this is not a universal latency claim"
    if performance_ok
    else mode_errors.get("performance", f"observations={performance_records!r}")
)

assertions = [
    result("ranking-behavior-preserved", functional_ok, functional_evidence),
    result("representative-speedup", performance_ok, performance_evidence),
]
print(
    json.dumps(
        {
            "passed": all(item["passed"] for item in assertions),
            "assertions": assertions,
            "metrics": metrics,
        }
    )
)
