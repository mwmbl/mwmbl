import argparse
import logging

from mwmbl.pipeline.config.config import parse_config_file
from mwmbl.pipeline.connections.connection_group_handler import init_global_connections_handler
from mwmbl.pipeline.pipeline import Pipeline

logging.basicConfig()


def setup_args():
    """Read all the args."""
    parser = argparse.ArgumentParser(description="mwmbl-pipeline")
    parser.add_argument("--config", help="Path to pipeline's yaml config.", required=True)
    parser.add_argument(
        "--validate-config",
        help="Whether to only validate config.",
        required=False,
        default=False,
        action='store_true'
    )
    args = parser.parse_args()
    return args


def main():
    """Main entrypoint for tinysearchengine.

    * Parses CLI args
    * Parses and validates config
    * Initializes connections
    * Initializes pipeline
    * Run pipeline
    """
    args = setup_args()
    config = parse_config_file(config_filename=args.config)

    if args.validate_config:
        print("Config validated successfully.")
        return

    # Initialize global connections handler
    conn_group_config = config.get("conn_group_config", None)
    init_global_connections_handler(conn_group_config=conn_group_config)

    # Initialize pipeline
    pipeline_config = config.get("pipeline")
    pipeline = Pipeline(**pipeline_config)

    # Run
    pipeline.run()


if __name__ == "__main__":
    # Run using `python -m mwmbl.pipeline.main --config config/pipeline/pipeline_dummy.yaml`
    main()
