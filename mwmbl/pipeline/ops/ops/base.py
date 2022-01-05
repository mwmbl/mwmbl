from __future__ import annotations

import abc
from typing import Type

from pydantic import BaseModel

from mwmbl.pipeline.messages.std_data import StdData


class BaseOpModel(BaseModel):
    """The base pydantic model used to validate the config or params of any Op.

    - Preferably "forbid" extra arguments.
    - Preferably do not allow "arbitrary_types". Stick to base types which are JSON compatible.
    """
    pass


class BaseOp(abc.ABC):
    """A Base Operation class that must be inherited by all Op subclasses.

    Subclasses must implement or declare the following abstractmethods and variables:
    - OP_TYPE: str
    - OP_MODEL: Type[BaseOpModel]
    - run(self, data: StdData) -> StdData

    * It can be used to enforce certain abstractmethods and variables

    Example declaration of subclass:

        class DummyOp(BaseOp):
            OP_TYPE = "dummy_op"
            OP_MODEL = DummyOpModel

            def run(self) -> StdData:
                return StdData(data="dummy")
    """
    OP_TYPE: str
    """The class variable that needs to be set for an Op subclass to identify the Op.
    - The op_type must be unique across all subclasses
    """

    OP_MODEL: Type[BaseOpModel]
    """The class variable that needs to be set for an Op subclass to identify the OpModel.
    - The OpModel is used to validate the params or config passed to the Op subclass.
    """

    @abc.abstractmethod
    def run(self, data: StdData) -> StdData:
        """The method to be implemented by all subclasses."""
        raise NotImplementedError()




