#!/usr/bin/env python3
"""External state-ownership and Go race-detector oracle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.environ["EVAL_SHARED_ROOT"])
from go_external_oracle import (  # noqa: E402
    GoModulePolicy,
    GoOracleError,
    build_external_go_oracle,
    run_go_mode,
)


def assertion(identifier: str, passed: bool, evidence: str) -> dict[str, object]:
    return {"id": identifier, "passed": passed, "evidence": evidence}


def strict_integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


workspace = Path(os.environ["EVAL_WORKSPACE"]).resolve()
policy = GoModulePolicy(
    module_path="example.com/counterstore",
    package_name="counterstore",
    required_source="store.go",
    api_contract="counterstore-v1",
)
harness_source = r"""package main

import (
	"encoding/json"
	"flag"
	"os"
	"sort"
	"runtime"
	"sync"

	candidate "example.com/counterstore"
)

const protocol = "external-go-oracle-v1"

type envelope struct {
	Protocol     string `json:"protocol"`
	Token        string `json:"token"`
	Mode         string `json:"mode"`
	Complete     bool   `json:"complete"`
	Observations any    `json:"observations"`
}

type concurrentObservations struct {
	IncrementReturns []int `json:"increment_returns"`
	Shared           int   `json:"shared"`
}

type ownershipObservations struct {
	KeptAfterMutation     int  `json:"kept_after_mutation"`
	InjectedAfterMutation int  `json:"injected_after_mutation"`
	SecondKept            int  `json:"second_kept"`
	SecondSize            int  `json:"second_size"`
	CoherenceChecks       int  `json:"coherence_checks"`
	CoherenceFailures     int  `json:"coherence_failures"`
	ConcurrentMovement    bool `json:"concurrent_movement"`
	FirstIncrement        int  `json:"first_increment"`
	SecondIncrement       int  `json:"second_increment"`
	ValidTransfer         bool `json:"valid_transfer"`
	LeftAfterValid        int  `json:"left_after_valid"`
	RightAfterValid       int  `json:"right_after_valid"`
	InsufficientTransfer  bool `json:"insufficient_transfer"`
	NonpositiveTransfer   bool `json:"nonpositive_transfer"`
	SameKeyTransfer       bool `json:"same_key_transfer"`
	LeftAfterRejected     int  `json:"left_after_rejected"`
	RightAfterRejected    int  `json:"right_after_rejected"`
	RepeatedTransfers     []bool `json:"repeated_transfers"`
	LeftAfterRepeated     int  `json:"left_after_repeated"`
	RightAfterRepeated    int  `json:"right_after_repeated"`
	ConcurrentSuccesses   int  `json:"concurrent_successes"`
	FinalLeft             int  `json:"final_left"`
	FinalRight            int  `json:"final_right"`
	WorkerCompleted       bool `json:"worker_completed"`
}

type raceObservations struct {
	Completed bool `json:"completed"`
}

func observeConcurrent() concurrentObservations {
	store := candidate.NewStore()
	const workers = 24
	const increments = 1500
	returns := make([]int, workers*increments)
	var wait sync.WaitGroup
	for worker := 0; worker < workers; worker++ {
		wait.Add(1)
		go func(worker int) {
			defer wait.Done()
			for index := 0; index < increments; index++ {
				returns[worker*increments+index] = store.Increment("shared")
			}
		}(worker)
	}
	wait.Wait()
	sort.Ints(returns)
	return concurrentObservations{IncrementReturns: returns, Shared: store.Get("shared")}
}

func observeOwnership() ownershipObservations {
	store := candidate.NewStore()
	store.Increment("kept")
	store.Increment("kept")
	snapshot := store.Snapshot()
	snapshot["kept"] = 999
	snapshot["injected"] = 1
	delete(snapshot, "missing")
	result := ownershipObservations{
		KeptAfterMutation:     store.Get("kept"),
		InjectedAfterMutation: store.Get("injected"),
	}
	semantics := candidate.NewStore()
	result.FirstIncrement = semantics.Increment("left")
	result.SecondIncrement = semantics.Increment("left")
	for index := 0; index < 3; index++ {
		semantics.Increment("left")
	}
	result.ValidTransfer = semantics.Transfer("left", "right", 3)
	result.LeftAfterValid = semantics.Get("left")
	result.RightAfterValid = semantics.Get("right")
	result.InsufficientTransfer = semantics.Transfer("left", "right", 3)
	result.NonpositiveTransfer = semantics.Transfer("left", "right", 0)
	result.SameKeyTransfer = semantics.Transfer("left", "left", 1)
	result.LeftAfterRejected = semantics.Get("left")
	result.RightAfterRejected = semantics.Get("right")
	result.RepeatedTransfers = []bool{
		semantics.Transfer("right", "left", 2),
		semantics.Transfer("left", "right", 1),
		semantics.Transfer("left", "right", 2),
	}
	result.LeftAfterRepeated = semantics.Get("left")
	result.RightAfterRepeated = semantics.Get("right")
	second := store.Snapshot()
	result.SecondKept = second["kept"]
	result.SecondSize = len(second)

	const total = 2000
	coherent := candidate.NewStore()
	for index := 0; index < total; index++ {
		coherent.Increment("left")
	}
	done := make(chan struct{})
	moved := make(chan struct{})
	observed := make(chan struct{})
	go func() {
		defer close(done)
		if coherent.Transfer("left", "right", 1) {
			result.ConcurrentSuccesses++
			close(moved)
			<-observed
		}
		for index := 1; index < 4000; index++ {
			var transferred bool
			if index%2 == 1 {
				transferred = coherent.Transfer("right", "left", 1)
			} else {
				transferred = coherent.Transfer("left", "right", 1)
			}
			if transferred {
				result.ConcurrentSuccesses++
			}
		}
	}()
	<-moved
	firstMovement := coherent.Snapshot()
	result.ConcurrentMovement = firstMovement["left"] == total-1 && firstMovement["right"] == 1
	close(observed)
	for {
		current := coherent.Snapshot()
		result.CoherenceChecks++
		if current["left"] < 0 || current["right"] < 0 || current["left"]+current["right"] != total {
			result.CoherenceFailures++
		}
		select {
		case <-done:
			final := coherent.Snapshot()
			result.FinalLeft = final["left"]
			result.FinalRight = final["right"]
			result.WorkerCompleted = true
			return result
		default:
			runtime.Gosched()
		}
	}
}

func observeRace() raceObservations {
	store := candidate.NewStore()
	const workers = 8
	const rounds = 800
	var wait sync.WaitGroup
	for worker := 0; worker < workers; worker++ {
		wait.Add(2)
		go func(id int) {
			defer wait.Done()
			for index := 0; index < rounds; index++ {
				store.Increment("shared")
				store.Increment(string(rune('a' + id)))
				_ = store.Transfer("shared", "moved", 1)
				_ = store.Transfer("moved", "shared", 1)
			}
		}(worker)
		go func() {
			defer wait.Done()
			for index := 0; index < rounds; index++ {
				for range store.Snapshot() {
				}
				_ = store.Get("shared")
				runtime.Gosched()
			}
		}()
	}
	wait.Wait()
	return raceObservations{Completed: true}
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
	case "concurrent":
		observations = observeConcurrent()
	case "ownership":
		observations = observeOwnership()
	case "race":
		observations = observeRace()
	default:
		os.Exit(2)
	}
	if err := json.NewEncoder(os.Stdout).Encode(envelope{
		Protocol: protocol, Token: *token, Mode: *mode, Complete: true,
		Observations: observations,
	}); err != nil {
		os.Exit(3)
	}
}
"""

mode_results: dict[str, dict[str, object]] = {}
mode_errors: dict[str, str] = {}
BUILD_TIMEOUT_SECONDS = 90
MODE_TIMEOUT_SECONDS = 18
try:
    with build_external_go_oracle(
        workspace,
        policy,
        harness_source,
        race=True,
        timeout_seconds=BUILD_TIMEOUT_SECONDS,
    ) as built:
        for mode in ("concurrent", "ownership", "race"):
            try:
                mode_results[mode] = run_go_mode(
                    built, mode, MODE_TIMEOUT_SECONDS
                ).observations
            except GoOracleError as error:
                mode_errors[mode] = str(error)
except (GoOracleError, OSError, ValueError) as error:
    detail = f"external Go harness failed: {type(error).__name__}: {error}"
    mode_errors = {mode: detail for mode in ("concurrent", "ownership", "race")}

concurrent = mode_results.get("concurrent", {})
increment_returns = concurrent.get("increment_returns")
concurrent_ok = (
    set(concurrent) == {"increment_returns", "shared"}
    and strict_integer(concurrent.get("shared")) == 24 * 1500
    and isinstance(increment_returns, list)
    and len(increment_returns) == 24 * 1500
    and all(
        strict_integer(value) == expected
        for expected, value in enumerate(increment_returns, start=1)
    )
)
concurrent_evidence = (
    "all concurrent increments were retained"
    if concurrent_ok
    else mode_errors.get("concurrent", f"observations={concurrent!r}")
)

ownership = mode_results.get("ownership", {})
ownership_ok = (
    set(ownership)
    == {
        "coherence_checks",
        "coherence_failures",
        "concurrent_movement",
        "concurrent_successes",
        "final_left",
        "final_right",
        "first_increment",
        "injected_after_mutation",
        "insufficient_transfer",
        "kept_after_mutation",
        "left_after_rejected",
        "left_after_repeated",
        "left_after_valid",
        "nonpositive_transfer",
        "repeated_transfers",
        "right_after_rejected",
        "right_after_repeated",
        "right_after_valid",
        "same_key_transfer",
        "second_increment",
        "second_kept",
        "second_size",
        "valid_transfer",
        "worker_completed",
    }
    and strict_integer(ownership.get("kept_after_mutation")) == 2
    and strict_integer(ownership.get("injected_after_mutation")) == 0
    and strict_integer(ownership.get("second_kept")) == 2
    and strict_integer(ownership.get("second_size")) == 1
    and strict_integer(ownership.get("first_increment")) == 1
    and strict_integer(ownership.get("second_increment")) == 2
    and ownership.get("valid_transfer") is True
    and strict_integer(ownership.get("left_after_valid")) == 2
    and strict_integer(ownership.get("right_after_valid")) == 3
    and ownership.get("insufficient_transfer") is False
    and ownership.get("nonpositive_transfer") is False
    and ownership.get("same_key_transfer") is True
    and strict_integer(ownership.get("left_after_rejected")) == 2
    and strict_integer(ownership.get("right_after_rejected")) == 3
    and ownership.get("repeated_transfers") == [True, True, True]
    and strict_integer(ownership.get("left_after_repeated")) == 1
    and strict_integer(ownership.get("right_after_repeated")) == 4
    and ownership.get("concurrent_movement") is True
    and strict_integer(ownership.get("concurrent_successes")) == 4000
    and strict_integer(ownership.get("final_left")) == 2000
    and strict_integer(ownership.get("final_right")) == 0
    and (strict_integer(ownership.get("coherence_checks")) or 0) > 0
    and strict_integer(ownership.get("coherence_failures")) == 0
    and ownership.get("worker_completed") is True
)
ownership_evidence = (
    "snapshot ownership and coupled-state coherence both held"
    if ownership_ok
    else mode_errors.get("ownership", f"observations={ownership!r}")
)

race = mode_results.get("race", {})
race_ok = set(race) == {"completed"} and race.get("completed") is True
race_evidence = (
    "the external race-instrumented harness found no shared-memory race"
    if race_ok
    else mode_errors.get("race", f"observations={race!r}")
)

assertions = [
    assertion("concurrent-state-correctness", concurrent_ok, concurrent_evidence),
    assertion("snapshot-state-ownership", ownership_ok, ownership_evidence),
    assertion("race-detector-clean", race_ok, race_evidence),
]
print(
    json.dumps(
        {"passed": all(item["passed"] for item in assertions), "assertions": assertions}
    )
)
