from pydantic import BaseModel, validator, root_validator, StrictInt, StrictStr, Field
from typing import Union, Optional, List, Dict, Tuple, Any, Set, TextIO, BinaryIO
from typing_extensions import Literal
import yaml
import pathlib


class ServerConfigModel(BaseModel):
    host: StrictStr = "0.0.0.0"
    port: StrictInt = 8080
    log_level: StrictStr = "info"


class IndexConfigModel(BaseModel):
    index_path: StrictStr = "data/index.tinysearch"
    num_pages: StrictInt = 25600
    page_size: StrictInt = 4096


class ConfigModel(BaseModel):
    server_config: ServerConfigModel = Field(default_factory=ServerConfigModel)
    index_config: IndexConfigModel = Field(default_factory=IndexConfigModel)


def parse_config_file(config_filename: str) -> ConfigModel:
    """Parse config dictionary and return ConfigModel."""
    if not pathlib.Path(config_filename).is_file():
        raise ValueError(
            f"config_filename: {config_filename} is not a file. Please check if it exists."
        )

    with open(config_filename) as f:
        config = yaml.load(f, yaml.Loader)

    return ConfigModel(**config)


if __name__ == "__main__":
    # Call this from the root of the repo using "python -m mwmbl.tinysearchengine.config"
    config_model = parse_config_file(config_filename="config/tinysearchengine.yaml")
    print(config_model.dict())
