from mwmbl.pipeline.messages.std_data import StdData
from mwmbl.pipeline.ops.ops.base import BaseOp, BaseOpModel


class NoneOpModel(BaseOpModel):
    # No config params

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = False


class NoneOp(BaseOp):
    OP_TYPE = "none_op"
    OP_MODEL = NoneOpModel

    def __init__(self):
        """Initialize."""
        pass

    def run(self, data: StdData) -> StdData:
        """Return a StdData with no data."""
        print("NoneOp: Running")
        return StdData(data=None)
