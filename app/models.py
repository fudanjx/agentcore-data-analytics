import uuid
from typing import Literal

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]


class ResponseMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: Literal["stop"] = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    model: str
    choices: list[Choice]
    usage: Usage = Usage()

    @classmethod
    def from_text(cls, text: str, model: str) -> "ChatCompletionResponse":
        return cls(
            id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
            model=model,
            choices=[Choice(message=ResponseMessage(content=text))],
        )
