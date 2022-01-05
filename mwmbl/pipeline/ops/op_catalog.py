"""Manually managed & curated Op types.

1. First import a new Op that you have implemented.
2. Add the Op class to the OP_CLASSES list.
"""
from collections import defaultdict
from typing import List, Dict
from typing import Type

from .ops.base import BaseOp
from .ops.commoncrawl.download_cc_index import DownloadCCIndex
from .ops.none_op import NoneOp

# --------------------------------------------------------------------------------------------------
# Add your implemented Op class to the OP_CLASSES list
# --------------------------------------------------------------------------------------------------
# The list can contain any class that is a subclass of BaseOp
OP_CLASSES: List[Type[BaseOp]] = [
    NoneOp,
    DownloadCCIndex,
]
# --------------------------------------------------------------------------------------------------


def _validate_no_duplicate_op_types(op_classes):
    """Validate that there are no duplicate op_types.

    This check is expected to run at runtime, every time the module is loaded for the first time.
    """
    # If a key doesn't exist in the dict, it is initialized with an empty list.
    op_type_to_class_map = defaultdict(list)

    for op_class in op_classes:
        op_type_to_class_map[op_class.OP_TYPE].append(op_class)

    # str(v) -> returns the __repr__ of the Op class.
    op_type_count_offenders = {k: str(v) for k, v in op_type_to_class_map.items() if len(v) > 1}

    if op_type_count_offenders:
        offenders = [
            f"* OP_TYPE: '{op_type}' is duplicated for these OP_CLASSES: {op_classes}"
            for op_type, op_classes in op_type_count_offenders.items()
        ]
        offenders_str = "\n".join(offenders)

        # This is an important validation that gets checked at runtime.
        # A verbose error message is justified.
        raise ValueError(
            f"The OP_CATALOG cannot be contructed since there are some Ops that have duplicated "
            f"`op_types`. Please make sure that the `op_types` are unique across all Ops. The "
            f"following is the list duplicate `op_types` and their corresponding Op classes:\n"
            f"{offenders_str}"
        )


_validate_no_duplicate_op_types(OP_CLASSES)

# Finally, construct the OP_CATALOG which is a mapping from Op.OP_TYPE -> Op
# Given an `op_type`, the OP_CATALOG will tell you the corresponding Op class.
OP_CATALOG: Dict[str, Type[BaseOp]] = {op_class.OP_TYPE: op_class for op_class in OP_CLASSES}


