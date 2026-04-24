"""Custom Ninja parser for Protobuf requests"""
from ninja.parser import Parser
from google.protobuf.message import DecodeError, Message
from google.protobuf.json_format import MessageToDict
from typing import Type
import logging

logger = logging.getLogger(__name__)


class ProtobufParser(Parser):
    """
    Custom parser for binary Protobuf data in Ninja endpoints.
    
    Parses binary Protobuf from request body and converts to dict
    for Ninja's Pydantic schema validation.
    """
    
    def __init__(self, proto_class: Type[Message]):
        """
        Initialize parser with Protobuf message class.
        
        Args:
            proto_class: The Protobuf message class to parse
        """
        self.proto_class = proto_class
    
    def parse_body(self, request):
        """
        Parse binary Protobuf from request body.
        
        Args:
            request: Django/Ninja request object
            
        Returns:
            dict: Parsed Protobuf message as dictionary
            
        Raises:
            ValueError: If Protobuf data is invalid
        """
        try:
            # Create message instance
            msg = self.proto_class()
            
            # Parse binary data
            msg.ParseFromString(request.body)
            
            # Convert to dict for Ninja/Pydantic
            # preserving_proto_field_name=True keeps snake_case field names
            result = MessageToDict(
                msg,
                preserving_proto_field_name=True,
                including_default_value_fields=False
            )
            
            logger.debug(f"Successfully parsed {self.proto_class.__name__}")
            return result
            
        except DecodeError as e:
            logger.error(f"Failed to decode Protobuf: {e}")
            raise ValueError(f"Invalid Protobuf data: {e}")
        except Exception as e:
            logger.error(f"Unexpected error parsing Protobuf: {e}")
            raise ValueError(f"Error parsing Protobuf: {e}")


class ProtobufParserFactory:
    """Factory for creating Protobuf parsers"""
    
    @staticmethod
    def create(proto_class: Type[Message]) -> ProtobufParser:
        """Create a parser for the given Protobuf class"""
        return ProtobufParser(proto_class)
