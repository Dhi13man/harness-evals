from __future__ import annotations

import unittest

from harness_evals.artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    normalize_artifact,
)


class ArtifactNormalizationTests(unittest.TestCase):
    def test_text_and_json_normalization_have_fixed_evidence(self) -> None:
        text = normalize_artifact("final_output_text", "line 1\r\nline 2\r")
        self.assertEqual(text.content, b"line 1\nline 2\n")
        self.assertEqual(
            text.as_evidence(),
            {
                "byte_count": 14,
                "canonicalization": "harness-evals-text-lf-v1",
                "filename": "artifact.txt",
                "kind": "final_output_text",
                "media_type": "text/plain; charset=utf-8",
                "raw_byte_count": 15,
                "sha256": "9060554863a62b9db5f726216876654e561896071d2e6480f2048b70e0fdadb9",
            },
        )

        first = normalize_artifact("final_output_json", '{"b":2, "a":1}')
        second = normalize_artifact("final_output_json", '{\n"a":1.0,"b":2\n}')
        self.assertEqual(first.content, b'{"a":1,"b":2}')
        self.assertEqual(second.content, first.content)
        self.assertEqual(
            first.sha256,
            "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
        )
        self.assertEqual(first.filename, "artifact.json")
        self.assertEqual(first.media_type, "application/json")
        self.assertEqual(first.canonicalization, "rfc8785")

    def test_raw_and_normalized_artifacts_are_bounded(self) -> None:
        exact = normalize_artifact("final_output_text", "x" * MAX_ARTIFACT_BYTES)
        self.assertEqual(exact.byte_count, MAX_ARTIFACT_BYTES)
        with self.assertRaisesRegex(ArtifactError, "raw size limit"):
            normalize_artifact("final_output_text", "x" * (MAX_ARTIFACT_BYTES + 1))

    def test_malformed_and_structurally_unsafe_outputs_are_rejected(self) -> None:
        too_deep = "[" * 65 + "0" + "]" * 65
        too_many_members = "[" + ",".join("0" for _ in range(10_001)) + "]"
        too_long_string = '{"value":"' + "x" * (256 * 1024 + 1) + '"}'
        too_long_number = '{"value":' + "1" * 129 + "}"
        cases = {
            "BOM": ("final_output_text", b"\xef\xbb\xbftext"),
            "invalid UTF-8": ("final_output_text", b"\xff"),
            "duplicate key": ("final_output_json", '{"a":1,"a":2}'),
            "non-finite": ("final_output_json", '{"a":NaN}'),
            "trailing prose": ("final_output_json", '{"a":1} prose'),
            "fenced extraction": ("final_output_json", '```json\n{"a":1}\n```'),
            "scalar": ("final_output_json", "1"),
            "depth": ("final_output_json", too_deep),
            "members": ("final_output_json", too_many_members),
            "string": ("final_output_json", too_long_string),
            "number": ("final_output_json", too_long_number),
        }
        for label, arguments in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ArtifactError):
                    normalize_artifact(*arguments)

    def test_content_drift_and_unknown_kinds_fail_closed(self) -> None:
        artifact = normalize_artifact("workspace_diff", "diff\n")
        artifact.assert_content(b"diff\n")
        with self.assertRaisesRegex(ArtifactError, "content drifted"):
            artifact.assert_content(b"changed\n")
        with self.assertRaisesRegex(ArtifactError, "unsupported artifact kind"):
            normalize_artifact("provider_claimed_output", "value")


if __name__ == "__main__":
    unittest.main()
