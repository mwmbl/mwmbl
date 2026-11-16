"""The `mwmbl` command."""

import argparse

from mwmbl.crawler import benchmark


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mwmbl")
    commands = parser.add_subparsers(dest="command", required=True)
    benchmark_parser = commands.add_parser(
        "benchmark", help="Find the crawler's best process and thread counts for this machine"
    )
    benchmark.add_arguments(benchmark_parser)
    benchmark_parser.set_defaults(run=benchmark.run)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
