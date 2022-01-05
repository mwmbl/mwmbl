# Pipeline Architecture

## Preface

`mwmbl` requires several distinct operations to be run for everything to come together. Some of these operations can be run independently, sequentially or in parallel. Some of these operations may also have different & sometimes conflicting dependencies. Knowing which operations to run and in which order might be hard to decipher by just reading the source code. Therefore, we introduce the concept of a pipeline by which we explicitly state which parts of the code need to run and in which order they need to run. The aim is to make it easy for the user to understand the flow of the code while also making it easy for the developer to develop new features or operations in an isolated/modular way. This architecture will also help with experimenting and trying out new strategies for different parts of `mwmbl` such as crawling, indexing, ranking etc. without affecting existing operations and workflow.  

The pipeline architecture is intended to help `mwmbl` along its path to planet scale but might not end up in the final version. This architecture introduces some small overhead in each operation, trading some performance for increased usability & readability. If the architecture doesn't help the developers as much as it was intended to, it will be removed for something better.


## Architecture

* Simply put, a pipeline is an ordered `list` of operations or `Ops`.
* Each `Op` in the list is called via `op.run()` one after the next. That's it.
* The pseudo-code that summarizes the pipeline is as follows:
    ```
    config = parse_config("config.yaml")
    list_of_ops = initialize_ops(config)
    for op in list_of_ops:
        op.run()
    ```
* There is some glue and validation code which is organized into hierarchical modules. This code while complicated was written to be futureproof to be extended in the future. However, it does not need to be altered unless there is a change in the architecture. A user may choose it ignore it and a developer need only understand it.
* Lastly, there are `Connections`.
    * These are intended to be managed connections to resources such as object storage, databases etc.
    * Since multiple `Ops` might want to connect to the same object storage bucket or database server, it makes sense to have a centralized way of managing the connections and the `Ops` can share a single `Connection` object.
    * An `Op` can access any number of `Connections` as it needs to.


## Configuration

* The pipeline is intended to be fully configured from a Yaml config file.
* The config file must have the following configurations
    * Define the order of the `Ops` to run i.e. pipeline definition as a list.
    * Each `Op` configuration includes:
        * The unique name or id of the operation i.e. `op_name` 
        * The type of operation it is i.e. `op_type`
        * The parameters or arguments passed to the operation i.e. `op_config` 
    * Define a list of `Connections` and their configurations. Each `Connection` configuration includes:
        * The unique name or id of the connection i.e. `conn_name`
        * The type of connection it is i.e. `conn_type`
        * The parameters or arguments passed to the connection i.e. `config_config`
* The entire config is validated as strictly as possible by pydantic models that are defined for each `Op` and `Connection`. This will let you find mistakes in the config right at start of run-time instead of in the middle of long-running pipelines.
* You can validate the config by running the following command:
    ```
    mwmbl-pipeline --config config/my_config.yaml --validate-config
    ``` 


## Running a pipeline
* First write a config file in Yaml format to fully describe your pipeline.
* Next run the following command:
    ```
    mwmbl-pipeline --config config/my_config.yaml
    ```
  

## Development Guide Using An Example

Let's assume you wanted to create a `DummyOp` which gets some random number data from a `DummyConnection` and write it to a file in the `/tmp` directory. You can follow the steps below that are involved in developing this `DummyOp`.

Keep in mind that your `Op` is not forced to use a `Connection`, it is just a common scenario that an `Op` might need to interact with an external resource.


#### Step 1: Create a DummyConnection
* Create a new file called `connections.connections.dummy_conn.py` and copy the following skeleton content into it. 
    ```
    from mwmbl.pipeline.connections.connections.base import BaseConnection, BaseConnectionModel

    class DummyConnectionModel(BaseConnectionModel):
        pass
    
    class DummyConnection(BaseConnection):
        def __init__(self):
            pass
    ```
* The convention is that we use `<NAME>Connection -> DummyConnection` for naming the Connection class and `<Name>ConnectionModel -> DummyConnectionModel` for naming the config validation class.
* The `BaseConnection` class is an abstract class that enforces that the `DummyConnection` class must have two class variables `CONN_TYPE` and `CONN_MODEL`.
    * The `CONN_TYPE` must be a unique *humanreadable* string name that identifies the type of Connection. We will name the type as `dummy_conn`.
    * The `CONN_MODEL` must be `DummyConnectionModel` which is a pydantic model that is used to validate the arguments to the `DummyConnection`.
    ```
    from mwmbl.pipeline.connections.connections.base import BaseConnection, BaseConnectionModel

    class DummyConnectionModel(BaseConnectionModel):
        pass
    
    class DummyConnection(BaseConnection):
        CONN_TYPE = "dummy_conn"              # <--- add CONN_TYPE
        CONN_MODEL = DummyConnectionModel     # <--- add CONN_MODEL
  
        def __init__(self):
            pass
    ```
* Let's add some functionality to the `DummyConnection`. The developer is free to implement the functionality within the class however they choose.
    * Let the class take an argument `seed` of type `int` which will be used as the random seed.
    * Let the class expose a method called `get_random` that generates a random float using the seed.
    ```
    from mwmbl.pipeline.connections.connections.base import BaseConnection, BaseConnectionModel
    import random                             # <--- import random 

    class DummyConnectionModel(BaseConnectionModel):
        pass
    
    class DummyConnection(BaseConnection):
        CONN_TYPE = "dummy_conn"
        CONN_MODEL = DummyConnectionModel
  
        def __init__(self, seed: int):        # <--- add any argument
            self.seed = seed
  
        def get_random(self) -> float:        # <--- add any method
            random.seed(self.seed)
            return random.random()            # <--- return anything
    ```
* Now that we are done with `DummyConnection`, let's look at `DummyConnectionModel`, which is a subclass of `pydantic.BaseModel`. It validates the arguments passed to it to `DummyConnection`.
    * First, let's add the `seed` argument and enforce the type.
    * Next, let's make sure that no other arguments are allowed to be passed to it.
    ```
    from mwmbl.pipeline.connections.connections.base import BaseConnection, BaseConnectionModel
    import random
    from pydantic import StrictInt

    class DummyConnectionModel(BaseConnectionModel):
        seed: StrictInt                      # <--- Add allowed argument with type check
  
        class Config:                        # <--- No extra arguments allowed
            extra = "forbid"
            arbitrary_types_allowed = False 
        
    class DummyConnection(BaseConnection):
        CONN_TYPE = "dummy_conn"
        CONN_MODEL = DummyConnectionModel
        def __init__(self, seed: int):
            self.seed = seed
  
        def get_random(self) -> float:
            random.seed(self.seed)
            return random.random()
    ```
#### Step 2: Register DummyConnection in connection_catalog
* Now that `DummyConnection` is fully implemented, we need to register it in a catalog i.e. add it to a list of classes so that the code can recognize the `conn_type`.
* Goto `connections.connection_catalog.py` and update the `CONN_CLASSES` and `AnyConnection`.
    * Register in `CONN_CLASSES`, this will let the ConnectionHandler know that there is a new Connection type that can be validated and initialized.
    * Register in `AnyConnection`, this will let `Ops` and other parts of the code base recognize that `DummyConnection` is a valid type and you may get some assistance from type checkers.
    ``` 
    from mwmbl.pipeline.connections.connections.dummy_conn import DummyConnection
    # ^--- Import DummyConnection
  
    CONN_CLASSES: List[Type[BaseConnection]] = [
        NoneConnection,
        ...,
        ...,
        DummyConnection,        # <--- Add DummyConnection to CONN_CLASSES
    ]
  
    AnyConnection = Union[
            NoneConnection,
            ...,
            ...,
            DummyConnection,    # <--- Add DummyConnection to AnyConnection
    ]
    ```
#### Step 3: Add connection config to the config.yaml
* Create a `config/pipeline/pipeline_dummpy.yaml` and add the following content.
    * At run time, the code will determine that `dummy_conn` refers to the class DummyConnection
    * Then an instance of DummyConnection will be initialized with the `seed` argument.
    * This instance will be given a name or id `dummy_random`.
    ```
    conn_group_config:
      - conn_name: "dummy_random"   # <--- Set any humanreadable name. 
        conn_type: "dummy_conn"     # <--- CONN_TYPE 
        conn_config:
          seed: 10                  # <--- Set any seed for the random lib
    ```

#### Step 4: Create a DummyOp
* Creating a `DummyOp` is much the same as creating a `DummyConnection`. So we'll start of a with an advanced template. Create the file `ops.ops.dummy_op.py` with the following content:
    ```
    from mwmbl.pipeline.messages.std_data import StdData
    from mwmbl.pipeline.ops.ops.base import BaseOp, BaseOpModel    
    
    class DummyOpModel(BaseOpModel):
        pass
  
        class Config:                             # <--- No extra arguments allowed
            extra = "forbid"
            arbitrary_types_allowed = False
    
    
    class DummyOp(BaseOp):
        OP_TYPE = "dummy_op"                      # <--- Add OP_TYPE
        OP_MODEL = DummyOpModel                   # <--- Add OP_MODEL
  
        def __init__(self):
            pass
    ```
* The `DummyOp` is supposed to print out a random number generated by `DummyConnection`. The business logic is supposed to be implemented under the `run()` method that is enforced by the `BaseOp`.
    * In order for the `DummyOp` to access this Connection, we need to import the global `ConnectionsHandler` which we can access using the `get_global_connections_handler` function.
    ```
    from mwmbl.pipeline.messages.std_data import StdData
    from mwmbl.pipeline.ops.ops.base import BaseOp, BaseOpModel    
    from mwmbl.pipeline.connections.connection_group_handler import get_global_connections_handler
    # ^--- Import get_global_connections_handler
    
    class DummyOpModel(BaseOpModel):
        pass
  
        class Config:
            extra = "forbid"
            arbitrary_types_allowed = False
    
    class DummyOp(BaseOp):
        OP_TYPE = "dummy_op"
        OP_MODEL = DummyOpModel
  
        def __init__(self):
            conns_handler = get_global_connections_handler()  # <--- Get the globally initialized ConnectionsHandler
  
        def run(self, data: StdData) -> StdData:              # <--- Implement abstract method
            # TODO print random number
    ```
* From the earlier step, we know that the user has introduced a config for a `DummyConnection` in the config file.
    * We can refer to that instance of the `DummyConnection` via its `conn_name`.
    * We introduce an argument called `dummy_conn_name` with which the `DummyOp` can get access to the specific instance of `DummyConnection`.
    * We add `dummy_conn_name` to `DummyOpModel` for validation.
    * Finally, we use this instance to generate and print a random number.
    ```
    from mwmbl.pipeline.messages.std_data import StdData
    from mwmbl.pipeline.ops.ops.base import BaseOp, BaseOpModel    
    from mwmbl.pipeline.connections.connection_group_handler import get_global_connections_handler
    from pydantic import StrictStr
    # ^--- Import StrictStr
    
    class DummyOpModel(BaseOpModel):
        dummy_conn_name: StrictStr           # Add validation for dummy_conn_name
  
        class Config:
            extra = "forbid"
            arbitrary_types_allowed = False    
    
    class DummyOp(BaseOp):
        OP_TYPE = "dummy_op"
        OP_MODEL = DummyOpModel
  
        def __init__(self, dummy_conn_name: str):
            conns_handler = get_global_connections_handler()
            self.dummy_conn = conns_handler.get_conn(dummy_conn_name)  # <--- Get instance of DummyConnection using conn_name
  
        def run(self, data: StdData) -> StdData:
            random_number = self.dummy_conn.get_random()               # <--- Get random number from DummyConnection
            print(f"{random_number=}")                                 # <--- Print the random number
            return StdData(data=None)                                  # <--- Return an empty instance of StdData
    ```
  
#### Step 5: Register DummyOp in OP_CATALOG
* Now that `DummyOp` is fully implemented, we need to register it in a catalog i.e. add it to a list of classes so that the code can recognize the `op_type`.
* Goto `ops.op_catalog.py` and update the `OP_CLASSES`.
    * Register in `OP_CLASSES`, this will let the OpHandler know that there is a new Op type that can be validated and initialized.
    ``` 
    from mwmbl.pipeline.ops.ops.dummy_op import DummyOp
    # ^--- Import DummyOp
  
    OP_CLASSES: List[Type[BaseOp]] = [
        NoneOp,
        ...,
        ...,
        DummyOp,        # <--- Add DummyOp to OP_CLASSES
    ]
    ```
  
#### Step 6: Add op & pipeline config to the config.yaml
* To the `config/pipeline/pipeline_dummpy.yaml` and add the pipeline & op config. The final config file will look like the following:
    ```
    conn_group_config:
      - conn_name: "dummy_random"                 # ---v Use this conn_name below
        conn_type: "dummy_conn" 
        conn_config:
          seed: 10
  
    pipeline:
      pipeline_name: "dummy_pipeline"
      pipeline_notes: "An example pipeline to print a random number."
      pipeline_config:
        op_group_config:
          - op_type: "dummy_op"                   # OP_TYPE
            op_config:
              dummy_conn_name: "dummy_random"     # <--- Use the conn_name of the dummy_conn
    ```


#### Step 7: Install, Validate & Run Pipeline
Your code and config are now ready to run.
```
# Install the code
$ pip install .[pipeline]

# Validate pipeline config
# If errors occur, fix them and re-install the code
$ mwmbl-pipeline --config config/pipeline/pipeline_dummy.yaml --validate-config

# Run pipeline
$ mwmbl-pipeline --config config/pipeline/pipeline_dummy.yaml
```
