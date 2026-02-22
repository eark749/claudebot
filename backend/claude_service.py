"""Claude SDK integration - streams responses and yields session_id."""

from collections.abc import AsyncIterator

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk.types import StreamEvent


async def stream_chat(
    prompt: str,
    claude_session_id: str | None = None,
) -> AsyncIterator[tuple[str, str | None]]:
    """
    Stream Claude's response. Yields (event_type, data) tuples:
    - ("thinking", chunk) when model is thinking
    - ("text", chunk) for each text chunk
    - ("done", session_id) when complete (session_id may be None if not received)
    """
    options = ClaudeAgentOptions(
        allowed_tools=["WebSearch"],
        resume=claude_session_id if claude_session_id else None,
        include_partial_messages=True,  # Enable token-level streaming
    )
    session_id = None
    in_tool = False

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, SystemMessage):
            if getattr(message, "subtype", None) == "init":
                session_id = message.data.get("session_id") if message.data else None
        elif isinstance(message, StreamEvent):
            event = message.event
            event_type = event.get("type")
            if event_type == "content_block_start":
                content_block = event.get("content_block", {})
                if content_block.get("type") == "tool_use":
                    in_tool = True
            elif event_type == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta" and not in_tool:
                    yield ("text", delta.get("text", ""))
            elif event_type == "content_block_stop":
                in_tool = False
        elif isinstance(message, AssistantMessage):
            # With include_partial_messages, text comes via StreamEvent; only handle ThinkingBlock
            for block in message.content:
                if isinstance(block, ThinkingBlock):
                    yield ("thinking", block.thinking)
        elif isinstance(message, ResultMessage):
            session_id = getattr(message, "session_id", None) or session_id
            yield ("done", session_id)
            return

    yield ("done", session_id)
