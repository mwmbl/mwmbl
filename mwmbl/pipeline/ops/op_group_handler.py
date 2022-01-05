import uuid
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, StrictStr, conlist

from mwmbl.pipeline.messages.std_data import StdData
from mwmbl.pipeline.ops.op_handler import OpHandlerModel, OpHandler


class OpGroupHandlerModel(BaseModel):
    op_group_config: conlist(OpHandlerModel, min_items=1)
    # op_group_name is not required yet.
    op_group_name: Optional[StrictStr] = None
    op_group_notes: Optional[StrictStr] = None


class OpGroupHandler(object):

    def __init__(
            self,
            op_group_config: List[Dict[str, Any]],
            op_group_name: Optional[str] = None,
            op_group_notes: Optional[StrictStr] = None,
    ):
        """Initialize OpGroupHandler.

        - Initialize the individual Ops via OpHandlers
        - OpHandlers will take care of validating the config for each Op
        """
        self.op_group_name = op_group_name if op_group_name else str(uuid.uuid4())
        self.op_group_notes = op_group_notes
        self.op_group_config = op_group_config
        self.op_handlers = [
            OpHandler(**op_config) for op_config in self.op_group_config
        ]
        print(f"OpGroupHandler: Initialized. op_group_name: {self.op_group_name}")

    def run(self, data: StdData) -> StdData:
        """Run all ops."""
        op_output = data
        for op_handler in self.op_handlers:
            op_output = op_handler.run(data=op_output)


if __name__ == "__main__":
    config = {
        "op_group_config": [
            {
                "op_type": "none_op",
                "op_config": {}
            }
        ]
    }
    # Validate config
    OpGroupHandlerModel(**config)
    # Initialize OpGroupHandler
    op_group_handler = OpGroupHandler(**config)
    op_group_handler.run(data=StdData(data=None))
    print("Done")
