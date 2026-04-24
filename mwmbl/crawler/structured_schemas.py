"""Pydantic schemas for structured content API"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class StructuredContentSchema(BaseModel):
    """Schema for structured content (matches Protobuf)"""
    url: str
    term: Optional[str] = None
    docs_python: Optional[Dict[str, Any]] = None
    github: Optional[Dict[str, Any]] = None


class StructuredResultsRequestSchema(BaseModel):
    """Schema for structured results request"""
    api_key: str = Field(..., min_length=1)
    results: List[StructuredContentSchema]
    crawler_version: Optional[str] = None


class StructuredResultsResponseSchema(BaseModel):
    """Schema for structured results response"""
    status: str
    url: str
    results_count: int
