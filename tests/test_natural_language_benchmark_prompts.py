"""Public benchmark requests must remain natural-language only.

The assembly pipeline may use structured schemas internally, but these four
files are the user-facing evidence that no schema or source-code knowledge is
required to request an assembly.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_DIR = (
    ROOT / "mac_assembly" / "assembly_prompts" /
    "natural_language_benchmarks"
)
PROMPT_FILES = sorted(BENCHMARK_DIR.glob("[0-9][0-9]_*.md"))
DECOMPOSER_PROMPT = (
    ROOT / "mac_assembly" / "prompts" / "decomposer.md"
).read_text(encoding="utf-8")


class TestNaturalLanguageBenchmarkPrompts(unittest.TestCase):
    def test_exactly_four_public_benchmarks(self):
        self.assertEqual(
            len(PROMPT_FILES), 4,
            f"expected four natural-language benchmarks, got {PROMPT_FILES}",
        )

    def test_requests_contain_no_code_or_data_blocks(self):
        for path in PROMPT_FILES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("```", text)
                self.assertNotIn("{", text)
                self.assertNotIn("}", text)

    def test_requests_do_not_name_internal_pipeline_api(self):
        forbidden = (
            "partspec",
            "matespec",
            "assemblybrief",
            "matingplan",
            "selector",
            "axis_point",
            "target_x_mm",
            "target_y_mm",
            "target_z_mm",
            "axial_offset_mm",
            "translation_mm",
            "reuses_part_id",
            "base_body",
            "builder.params",
            "feature operator",
            "python",
            "json",
        )
        for path in PROMPT_FILES:
            lowered = path.read_text(encoding="utf-8").lower()
            with self.subTest(path=path.name):
                for token in forbidden:
                    self.assertNotIn(token, lowered)

    def test_requests_are_substantive_natural_language(self):
        for path in PROMPT_FILES:
            text = path.read_text(encoding="utf-8")
            words = re.findall(r"[A-Za-z]+", text)
            with self.subTest(path=path.name):
                self.assertGreaterEqual(len(words), 250)
                self.assertIn("millimetres", text.lower())
                self.assertIn("deliver", text.lower())

    def test_decomposer_owns_internal_translation(self):
        required = (
            "public, natural-language design brief",
            "never required to know or name",
            "Translate the user's ordinary mechanical language",
            "geometry reuse automatically",
        )
        for phrase in required:
            self.assertIn(phrase, DECOMPOSER_PROMPT)


if __name__ == "__main__":
    unittest.main()
