from dataclasses import dataclass
from enum import Enum


class Verdict(Enum):
    SAFE = "safe"
    UNCERTAIN = "uncertain"
    NSFW = "nsfw"


class ImageSourceUnavailable(Exception):
    """The image source asked us to back off, or could not be reached."""


@dataclass(frozen=True)
class SourceImage:
    """What a source says about one image. Only an approved one is ever returned to a user."""

    title: str
    page_url: str
    thumbnail_url: str
    thumbnail_width: int
    thumbnail_height: int
    description: str
    categories: tuple[str, ...]
    license_name: str
    license_url: str
    attribution: str
    rank: int

    @property
    def text(self) -> str:
        """Everything the text checks read, in one string."""
        return "\n".join([self.title, self.description, *self.categories])


@dataclass(frozen=True)
class ApprovedImage:
    title: str
    page_url: str
    thumbnail_url: str
    thumbnail_width: int
    thumbnail_height: int
    source: str
    license_name: str
    license_url: str
    attribution: str
