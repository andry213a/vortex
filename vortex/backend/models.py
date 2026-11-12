from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    secret: str = Field(min_length=1, max_length=512)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str


class AuthStatusResponse(BaseModel):
    auth_mode: str
    requires_login: bool
    allow_guest: bool
    guest_write: bool
    hostname: str
    port: int


class MeResponse(BaseModel):
    role: str
    auth_mode: str


class FileItem(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    modified: float
    ext: str
    kind: str
    editable: bool = False


class ListResponse(BaseModel):
    path: str
    parent: str | None
    items: list[FileItem]
    total: int


class SearchResponse(BaseModel):
    query: str
    items: list[FileItem]
    total: int
    truncated: bool = False


class FileReadResponse(BaseModel):
    path: str
    name: str
    content: str
    size: int
    encoding: str
    editable: bool


class MkdirRequest(BaseModel):
    path: str = ""
    name: str = Field(min_length=1, max_length=255)


class RenameRequest(BaseModel):
    path: str
    new_name: str = Field(min_length=1, max_length=255)


class WriteRequest(BaseModel):
    path: str
    content: str


class UploadInitRequest(BaseModel):
    filename: str
    size: int = Field(ge=0)
    target_dir: str = ""
    chunk_size: int | None = None


class UploadInitResponse(BaseModel):
    upload_id: str
    filename: str
    size: int
    chunk_size: int
    chunk_count: int
    received: list[int]
    target_dir: str


class UploadStatusResponse(BaseModel):
    upload_id: str
    filename: str
    size: int
    chunk_size: int
    chunk_count: int
    received: list[int]
    target_dir: str


class UploadChunkResponse(BaseModel):
    upload_id: str
    received: list[int]
    received_count: int
    chunk_count: int


class UploadCompleteResponse(BaseModel):
    path: str
    name: str
    size: int


class DiskInfo(BaseModel):
    total: int
    used: int
    free: int


class SystemInfoResponse(BaseModel):
    base_dir: str
    port: int
    auth_mode: str
    hostname: str
    uptime: int
    disk: DiskInfo
    platform: dict[str, str]
    version: str


class PortCheckResponse(BaseModel):
    port: int
    available: bool
    suggestion: int | None = None
    message: str = ""
