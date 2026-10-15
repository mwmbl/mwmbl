#!/usr/bin/env python3
"""Combine coverage.py XML and cargo-llvm-cov LCOV line coverage."""

import argparse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Coverage:
    covered: int
    total: int

    @property
    def percent(self) -> float:
        if self.total == 0:
            raise ValueError("Coverage report has no executable lines")
        return self.covered / self.total * 100


def read_python_coverage(path: Path) -> Coverage:
    root = ET.parse(path).getroot()
    return Coverage(
        covered=int(root.attrib["lines-covered"]),
        total=int(root.attrib["lines-valid"]),
    )


def read_rust_coverage(path: Path) -> Coverage:
    source_file: str | None = None
    line_hits: dict[tuple[str, int], int] = {}

    for raw_line in path.read_text().splitlines():
        if raw_line.startswith("SF:"):
            source_file = raw_line[3:]
        elif raw_line.startswith("DA:"):
            if source_file is None:
                raise ValueError(f"{path}: DA record before SF record")
            line_number, hits, *_ = raw_line[3:].split(",")
            key = (source_file, int(line_number))
            line_hits[key] = max(line_hits.get(key, 0), int(hits))
        elif raw_line == "end_of_record":
            source_file = None

    if not line_hits:
        raise ValueError(f"{path}: no Rust line coverage records")

    return Coverage(
        covered=sum(hits > 0 for hits in line_hits.values()),
        total=len(line_hits),
    )


def combine_coverage(python: Coverage, rust: Coverage) -> Coverage:
    return Coverage(
        covered=python.covered + rust.covered,
        total=python.total + rust.total,
    )


def format_coverage(name: str, coverage: Coverage) -> str:
    return f"{name}: {coverage.covered}/{coverage.total} lines ({coverage.percent:.2f}%)"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("python_xml", type=Path)
    parser.add_argument("rust_lcov", type=Path)
    args = parser.parse_args()

    python = read_python_coverage(args.python_xml)
    rust = read_rust_coverage(args.rust_lcov)
    combined = combine_coverage(python, rust)

    print(format_coverage("Python", python))
    print(format_coverage("Rust", rust))
    print(format_coverage("Combined", combined))
    print(f"TOTAL COVERAGE: {combined.percent:.2f}%")


if __name__ == "__main__":
    main()
