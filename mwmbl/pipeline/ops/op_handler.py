import uuid
from typing import Optional, Dict, Any, Type

from pydantic import root_validator, StrictStr

from mwmbl.pipeline.messages.std_data import StdData
from mwmbl.pipeline.ops.op_catalog import OP_CATALOG
from mwmbl.pipeline.ops.ops.base import BaseOp, BaseOpModel


class OpHandlerModel(BaseOpModel):
    op_type: StrictStr
    # Accepts emtpy dict
    op_config: Dict[StrictStr, Any]
    op_name: Optional[StrictStr] = None
    notes: Optional[StrictStr] = None

    @root_validator(pre=False)
    def validate_op_type_op_config(cls, values):
        """Validate op_type & op_config.

        - Make sure that op_type exists in the OP_CATALOG
        - Make sure that op_config is a valid config by initializing the OpModel for the
          corresponding Op class based on op_type.
        """
        op_type = values.get("op_type", None)
        op_config = values.get("op_config", None)

        if op_type not in OP_CATALOG.keys():
            raise ValueError(f"OP_TYPE: {op_type} does not exist in OP_CATALOG.")

        op_class = OP_CATALOG[op_type]
        op_model = op_class.OP_MODEL

        # Validate the op_config by just initializing the OpModel
        op_model(**op_config)

        return values

    class Config:
        extra = "forbid"
        arbitrary_types_allowed = False


class OpHandler(object):

    def __init__(
            self,
            op_type: str,
            op_config: Dict[str, Any],
            op_name: Optional[str] = None,
            notes: Optional[str] = None,
            validate_config: Optional[bool] = False,
    ):
        """Initialize the OpHandler which in turn initializes an insance of Op.

        - The OpHandler expects that the arguments have been passed through and validated by
          the OpHandlerModel. The OpHandler will not validate the params.
        - The OpHandler initializes the Op object using op_type and op_config.
        - op_type & op_config are intended to have JSON compatible types ONLY so that these params
          can be passed from a JSON/YAML config file.

        Args:
            op_type (str): The unique identifier of the Op subclass
            op_config (Dict[str, Any]): The JSON compatible config that can be used to initialize
                the Op subclass.
            op_name (str): The humanreadable and unique name or id of the of op. This is used to
              uniquely identify the Op in the pipeline list or DAG.
              - It currently plays no role.
              - The op_name can be used in the future to reference the StdOutputs uniquely.
              - If None, it will be set to a UUID4 cast to string.
            notes (str): Since the OpHandler & Op & Pipeline are intended to be declared in a
              JSON/YAML config file, it might come in handy to have a notes key which can store
              comments. Some JSON formats do not allow comments in the file and comments may also
              be lost when the JSON is transmitted over the wire or stored in a database.
        """
        self.op_name = op_name if op_name else str(uuid.uuid4())
        self.notes = notes

        if op_type not in OP_CATALOG:
            raise ValueError(
                f"{op_type=} not found in OP_CATALOG. Corresponding {op_name=}. "
                f"Please check if it was due to one of the following errors:\n"
                f"1. The {op_type=} might have a typo.\n"
                f"2. The {op_type=} & corresponding Op Class was not included in OP_CLASSES in "
                f"the source code."
            )

        self.op_type = op_type
        self.op_config = op_config

        self.op_class = OP_CATALOG[self.op_type]  # type: Type[BaseOp]
        self.op_model = self.op_class.OP_MODEL
        self.op = self.op_class(**self.op_config)

        print(f"OpHandler: Initialized. op_name: {self.op_name}, op_type: {self.op_type}")

    def run(self, data: StdData) -> StdData:
        """Call Op's run method."""
        return self.op.run(data=data)


if __name__ == "__main__":
    config = {
        "op_type": "none_op",
        "op_config": {}
    }
    # TODO: Why does Pycharm show the error "'null' extra fields not permitted". But it works.
    # validate config
    OpHandlerModel(**config)
    # Init OpHandler
    op_handler = OpHandler(**config)
    op_handler.run(data=StdData(data=None))
    print("Done")
