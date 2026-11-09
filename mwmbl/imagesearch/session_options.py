import onnxruntime


def cpu_options(threads: int) -> onnxruntime.SessionOptions:
    """Options for a model that shares a server with everything else.

    By default ONNX Runtime starts a thread per core, and its idle threads spin rather than sleep.
    Measured on a 12-core machine that burned about five times the CPU time of the work itself, so
    the thread count is chosen per model and idle threads sleep.
    """
    options = onnxruntime.SessionOptions()
    options.log_severity_level = 3
    options.intra_op_num_threads = threads
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return options
