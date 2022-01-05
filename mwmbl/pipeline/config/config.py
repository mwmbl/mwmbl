import json
import pathlib
from typing import List, Optional, Dict, Any

import yaml
from pydantic import BaseModel

from mwmbl.pipeline.connections.connection_handler import ConnectionHandlerModel
from mwmbl.pipeline.pipeline import PipelineModel


class ConfigModel(BaseModel):
    # Replcating conn_group_config to be exactly as ConnectionGroupHandlerModel
    conn_group_config: Optional[List[ConnectionHandlerModel]] = None
    pipeline: PipelineModel

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = False


def parse_config_file(config_filename: str) -> Dict[str, Any]:
    """Parse config dictionary and return the config as a dict."""
    if not pathlib.Path(config_filename).is_file():
        raise ValueError(
            f"config_filename: {config_filename} is not a file. Please check if it exists."
        )

    with open(config_filename) as f:
        config = yaml.load(f, yaml.Loader)

    # Validate
    ConfigModel(**config)

    return config


if __name__ == "__main__":
    # Call this from the root of the repo using "python -m mwmbl.pipeline.config.config"
    config_ = parse_config_file(config_filename="config/pipeline/pipeline_dummy.yaml")
    print(json.dumps(config_, indent=2))
