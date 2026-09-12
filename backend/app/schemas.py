from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Operation(str, Enum):
    ping = "ping"
    traceroute = "traceroute"
    bgp = "bgp"
    bgp_community = "bgp_community"
    bgp_aspath = "bgp_aspath"


class Protocol(str, Enum):
    http = "http"
    telnet = "telnet"


class HttpRequestConfig(BaseModel):
    url: str
    method: Literal["GET", "POST"] = "GET"
    query: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | str | None = None
    body_type: Literal["json", "form", "text"] = "json"
    response_type: Literal["text", "json"] = "text"
    json_path: str | None = None
    output_regex: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("A URL deve começar com http:// ou https://")
        return value


class HttpConfig(BaseModel):
    timeout: int = Field(default=20, ge=2, le=120)
    verify_tls: bool = True
    requests: dict[Operation, HttpRequestConfig]

    @model_validator(mode="after")
    def require_request(self):
        if not self.requests:
            raise ValueError("Configure ao menos uma operação HTTP")
        return self


class HyperglassConfig(BaseModel):
    hyperglass_like: Literal[True] = True
    base_url: str
    location: str = Field(min_length=1)
    query_types: dict[Operation, str]
    api_format: Literal["v1", "v2"] = "v2"
    vrf: str = "default"
    bootstrap_session: bool = False
    timeout: int = Field(default=30, ge=2, le=300)
    verify_tls: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if normalized.endswith("/api"):
            normalized = normalized[:-4]
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("A URL base deve começar com http:// ou https://")
        return normalized

    @field_validator("location")
    @classmethod
    def normalize_location(cls, value: str) -> str:
        return value.strip()

    @field_validator("vrf")
    @classmethod
    def normalize_vrf(cls, value: str) -> str:
        return value.strip() or "default"

    @field_validator("query_types")
    @classmethod
    def validate_query_types(cls, value: dict[Operation, str]) -> dict[Operation, str]:
        cleaned = {operation: query_type.strip() for operation, query_type in value.items()}
        if not cleaned:
            raise ValueError("Configure ao menos uma operação Hyperglass")
        if any(not query_type for query_type in cleaned.values()):
            raise ValueError("O queryType das operações Hyperglass não pode ficar vazio")
        return cleaned


class HyperglassDiscoveryInput(BaseModel):
    base_url: str
    timeout: int = Field(default=15, ge=2, le=300)
    verify_tls: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if normalized.endswith("/api"):
            normalized = normalized[:-4]
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("A URL base deve começar com http:// ou https://")
        return normalized


class TelnetConfig(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(default=23, ge=1, le=65535)
    username: str = ""
    password: str = ""
    username_prompt: str = r"(?i)(login|username)[: ]*$"
    password_prompt: str = r"(?i)password[: ]*$"
    prompt_regex: str = r"[>#]\s*$"
    pre_commands: list[str] = Field(default_factory=list)
    commands: dict[Operation, str]
    commands_v6: dict[Operation, str] = Field(default_factory=dict)
    quit_command: str = "exit"
    timeout: int = Field(default=20, ge=2, le=120)

    @model_validator(mode="after")
    def require_command(self):
        if not self.commands:
            raise ValueError("Configure ao menos um comando Telnet")
        return self


class LookingGlassInput(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    protocol: Protocol
    enabled: bool = True
    config: dict[str, Any]

    @model_validator(mode="after")
    def validate_config(self):
        if self.protocol == Protocol.http:
            if self.config.get("hyperglass_like"):
                HyperglassConfig.model_validate(self.config)
            else:
                HttpConfig.model_validate(self.config)
        else:
            TelnetConfig.model_validate(self.config)
        return self


class QueryInput(BaseModel):
    target: str = Field(min_length=1, max_length=256)
    operation: Operation
    looking_glass_ids: list[str] | None = None


class ResultStatus(str, Enum):
    success = "success"
    error = "error"
    unsupported = "unsupported"


class QueryResult(BaseModel):
    looking_glass_id: str
    looking_glass_name: str
    protocol: Protocol
    status: ResultStatus
    output: str
    duration_ms: int


class BatchResult(BaseModel):
    target: str
    operation: Operation
    started_at: str
    duration_ms: int
    results: list[QueryResult]
