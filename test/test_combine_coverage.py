from pathlib import Path

import pytest

from scripts.combine_coverage import Coverage, combine_coverage, read_python_coverage, read_rust_coverage


def test_read_python_coverage(tmp_path: Path):
    report = tmp_path / "coverage.xml"
    report.write_text('<coverage lines-valid="10" lines-covered="7" />')

    assert read_python_coverage(report) == Coverage(covered=7, total=10)


def test_read_rust_coverage_deduplicates_lines(tmp_path: Path):
    report = tmp_path / "coverage.lcov"
    report.write_text(
        "\n".join(
            [
                "SF:/project/src/lib.rs",
                "DA:10,0",
                "DA:11,2",
                "end_of_record",
                "SF:/project/src/lib.rs",
                "DA:10,1",
                "DA:11,0",
                "end_of_record",
                "SF:/project/src/text.rs",
                "DA:10,0",
                "end_of_record",
            ]
        )
    )

    assert read_rust_coverage(report) == Coverage(covered=2, total=3)


def test_read_rust_coverage_rejects_empty_report(tmp_path: Path):
    report = tmp_path / "coverage.lcov"
    report.write_text("TN:\n")

    with pytest.raises(ValueError, match="no Rust line coverage records"):
        read_rust_coverage(report)


def test_combined_coverage_weights_executable_lines():
    python = Coverage(covered=70, total=100)
    rust = Coverage(covered=18, total=20)

    combined = combine_coverage(python, rust)

    assert combined == Coverage(covered=88, total=120)
    assert combined.percent == pytest.approx(73.333333)
