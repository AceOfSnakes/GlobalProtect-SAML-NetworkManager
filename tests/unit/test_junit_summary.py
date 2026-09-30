"""
Tests for .github/scripts/junit_summary.py, which turns the junit file of a
unit test job into the Markdown shown when the job is opened from the checks of
a pull request ($GITHUB_STEP_SUMMARY).

Every positive test has negative counterparts: a failure is shown as a failure,
a missing or broken report is "no report" and never a crash or a failed job.

Run with: make test-unit  (or: python3 -m pytest tests/unit -v)
"""

import importlib.util
import os
import subprocess
import sys

import pytest

SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".github", "scripts", "junit_summary.py")
)
spec = importlib.util.spec_from_file_location("junit_summary", SCRIPT)
junit_summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(junit_summary)


def junit(tests, failures=0, errors=0, skipped=0, cases=""):
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites>'
        f'<testsuite name="pytest" errors="{errors}" failures="{failures}" skipped="{skipped}" tests="{tests}">'
        f"{cases}</testsuite></testsuites>"
    )


def failing(classname, name, tag="failure"):
    return f'<testcase classname="{classname}" name="{name}"><{tag} message="x">boom</{tag}></testcase>'


@pytest.fixture
def report(tmp_path):
    def write(content, title="non-root (Python 3.10)"):
        path = tmp_path / "junit.xml"
        path.write_text(content, encoding="utf-8")
        return junit_summary.summary(title, str(path))

    return write


class TestSummary:
    def test_counts_of_a_green_run(self, report):
        text = report(junit(tests=120, skipped=3))

        assert "### ✅ non-root (Python 3.10)" in text
        assert "| 117 | 0 | 3 | 0 |" in text
        assert "Failing" not in text and "❌" not in text

    def test_a_bare_testsuite_root_is_read_too(self, report):
        text = report("<testsuite tests='6' failures='1' errors='0' skipped='1'/>")

        assert "| 4 | 1 | 1 | 0 |" in text

    def test_several_suites_are_summed(self, report):
        text = report(
            "<testsuites><testsuite tests='5' failures='0' errors='0' skipped='1'/>"
            "<testsuite tests='4' failures='1' errors='1' skipped='0'/></testsuites>"
        )

        assert "| 6 | 1 | 1 | 1 |" in text

    def test_the_title_is_escaped(self, report):
        assert "x \\| \\[a\\]\\(" not in report(junit(1), title="x | [a](http://e) <b>")
        assert "<b>" not in report(junit(1), title="<b>")


class TestFailures:
    def test_failed_tests_show_a_cross_the_counts_and_the_names(self, report):
        cases = failing("tests.unit.test_a.TestA", "test_one") + failing("tests.unit.test_b", "test_two", "error")

        text = report(junit(tests=10, failures=1, errors=1, cases=cases))

        assert "### ❌ non-root (Python 3.10)" in text
        assert "| 8 | 1 | 0 | 1 |" in text
        assert "- `tests.unit.test_a.TestA::test_one`" in text
        assert "- `tests.unit.test_b::test_two`" in text

    def test_only_the_first_failures_are_listed(self, report):
        cases = "".join(failing("c", f"t{i}") for i in range(25))

        text = report(junit(tests=25, failures=25, cases=cases))

        assert text.count("- `c::t") == junit_summary.MAX_LISTED
        assert "- and 15 more" in text

    def test_a_test_name_cannot_break_out_of_the_code_span(self, report):
        text = report(junit(1, failures=1, cases=failing("c", "a`b")))

        assert "- `c::a'b`" in text

    def test_a_run_that_collected_nothing_is_a_warning_not_a_pass(self, report):
        text = report(junit(tests=0))

        assert "⚠️" in text and "✅" not in text
        assert "No test passed" in text

    def test_a_run_where_everything_was_skipped_is_not_a_pass(self, report):
        text = report(junit(tests=4, skipped=4))

        assert "✅" not in text


class TestNoReport:
    def test_a_missing_file_is_no_report(self, tmp_path):
        text = junit_summary.summary("root (Python 3.12)", str(tmp_path / "junit.xml"))

        assert "no report" in text and "did not get as far" in text
        assert "✅" not in text

    @pytest.mark.parametrize(
        "content",
        [
            "",
            "not xml at all",
            "<testsuites><testsuite tests='1'>",
            "<html></html>",
            "<testsuites></testsuites>",
            "<testsuite tests='many' failures='0' errors='0' skipped='0'/>",
            "<testsuite tests='-1' failures='0' errors='0' skipped='0'/>",
            "\x00\x01\x02",
            '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><testsuite tests="1">&a;</testsuite>',
        ],
    )
    def test_a_broken_report_is_no_report_and_not_a_crash(self, report, content):
        text = report(content)

        assert "no report: cannot read it" in text
        assert "✅" not in text and "| Passed |" not in text

    def test_an_oversized_report_is_refused(self, report, monkeypatch):
        monkeypatch.setattr(junit_summary, "MAX_BYTES", 10)

        assert "no report: cannot read it (file too large)" in report(junit(3))


class TestCommandLine:
    def run(self, *args):
        return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)

    def test_prints_the_summary(self, tmp_path):
        path = tmp_path / "junit.xml"
        path.write_text(junit(5))

        result = self.run("--title", "root (Python 3.x)", str(path))

        assert result.returncode == 0, result.stderr
        assert "### ✅ root (Python 3.x)" in result.stdout

    @pytest.mark.parametrize("content", [None, "garbage"])
    def test_a_missing_or_broken_report_never_fails_the_job(self, tmp_path, content):
        path = tmp_path / "junit.xml"
        if content is not None:
            path.write_text(content)

        result = self.run(str(path))

        assert result.returncode == 0, result.stderr
        assert "no report" in result.stdout
        assert "Traceback" not in result.stderr

    def test_the_file_argument_is_required(self):
        result = self.run()

        assert result.returncode != 0
        assert result.stdout == ""
