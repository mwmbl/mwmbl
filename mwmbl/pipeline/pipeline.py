import uuid
from typing import Optional, Dict, Any

from pydantic import BaseModel, StrictStr

from mwmbl.pipeline.messages.std_data import StdData
from mwmbl.pipeline.ops.op_group_handler import OpGroupHandler, OpGroupHandlerModel


class PipelineModel(BaseModel):
    pipeline_config: OpGroupHandlerModel
    pipeline_name: Optional[StrictStr] = None
    pipeline_notes: Optional[StrictStr] = None

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = False


class Pipeline(object):
    def __init__(
            self,
            pipeline_config: Dict[str, Any],
            pipeline_name: Optional[str] = None,
            pipeline_notes: Optional[str] = None
    ):
        """Initialize pipeline."""
        self.pipeline_name = pipeline_name if pipeline_name else str(uuid.uuid4())
        self.pipeline_notes = pipeline_notes
        self.pipeline_config = pipeline_config
        self.op_group_handler = OpGroupHandler(**self.pipeline_config)

        print(f"Pipeline: Initialized. pipeline_name: {self.pipeline_name}")

    def run(self):
        """Run all steps."""
        # Provide emtpy data to start off the pipeline.
        empty_data = StdData(data=None)
        self.op_group_handler.run(data=empty_data)


if __name__ == "__main__":
    config = {
        "pipeline_name": "dummy_pipeline",
        "pipeline_notes": "A minimal pipeline that works but does not do anything.",
        "pipeline_config": {
            "op_group_config": [
                {
                    "op_type": "none_op",
                    "op_config": {}
                }
            ]
        }
    }
    # Validate config
    PipelineModel(**config)
    # Initialize PipelineHandler
    Pipeline(**config)
