"""Request/response models for the index lifecycle endpoints."""

from pydantic import BaseModel


class ProvisionIndexResponse(BaseModel):
    """`created` is True when the index was newly created, False if it existed."""

    index: str
    created: bool


class DeleteIndexResponse(BaseModel):
    """`deleted` is True when an index was dropped, False if it did not exist."""

    index: str
    deleted: bool
