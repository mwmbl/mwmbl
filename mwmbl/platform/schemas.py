from datetime import datetime
from typing import Optional, Literal
from ninja import Schema, ModelSchema, Field

from mwmbl.models import DomainSubmission


class Registration(Schema):
    email: str
    username: str
    password: str


class ConfirmEmail(Schema):
    username: str
    email: str
    key: str


class DomainSubmissionSchema(ModelSchema):
    class Meta:
        model = DomainSubmission
        fields = ["id", "name", "submitted_by", "submitted_on", "status", "rejection_reason", "rejection_detail"]


class UpdateDomainSubmission(ModelSchema):
    class Meta:
        model = DomainSubmission
        fields = ["status", "rejection_reason", "rejection_detail"]


class VoteRequest(Schema):
    """Request schema for voting on search results."""
    
    url: str = Field(
        description="The URL of the search result being voted on",
        example="https://example.com/article"
    )
    query: str = Field(
        description="The search query that returned this result",
        example="python tutorial"
    )
    vote_type: Literal["upvote", "downvote"] = Field(
        description="Type of vote - either 'upvote' for positive feedback or 'downvote' for negative feedback",
        example="upvote"
    )


class VoteRemoveRequest(Schema):
    """Request schema for removing a vote on a search result."""
    
    url: str = Field(
        description="The URL of the search result to remove the vote from",
        example="https://example.com/article"
    )
    query: str = Field(
        description="The search query that returned this result",
        example="python tutorial"
    )


class VoteStats(Schema):
    """Statistics for votes on a specific search result."""
    
    upvotes: int = Field(
        description="Total number of upvotes for this search result",
        example=15
    )
    downvotes: int = Field(
        description="Total number of downvotes for this search result",
        example=3
    )
    user_vote: Optional[Literal["upvote", "downvote"]] = Field(
        default=None,
        description="The current user's vote on this result, if any",
        example="upvote"
    )


class VoteStatsRequest(Schema):
    """Request schema for getting vote statistics for multiple URLs."""
    
    query: str = Field(
        description="The search query that returned these results",
        example="python tutorial"
    )
    urls: list[str] = Field(
        description="List of URLs to get vote statistics for",
        example=["https://example.com/article", "https://another-site.com/page"]
    )


class VoteResponse(Schema):
    """Response schema containing vote statistics for multiple URLs."""
    
    votes: dict[str, VoteStats] = Field(
        description="Dictionary mapping URLs to their vote statistics",
        example={
            "https://example.com/article": {
                "upvotes": 15,
                "downvotes": 3,
                "user_vote": "upvote"
            },
            "https://another-site.com/page": {
                "upvotes": 8,
                "downvotes": 1,
                "user_vote": None
            }
        }
    )


class UserVoteHistory(Schema):
    """Schema representing a user's voting history entry."""
    
    url: str = Field(
        description="The URL of the search result that was voted on",
        example="https://example.com/article"
    )
    query: str = Field(
        description="The search query that returned this result",
        example="python tutorial"
    )
    vote_type: Literal["upvote", "downvote"] = Field(
        description="The type of vote cast by the user",
        example="upvote"
    )
    timestamp: datetime = Field(
        description="When the vote was cast",
        example="2024-01-15T10:30:00Z"
    )
