"""Context Budget Manager, Compression, and Caching."""
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
from app.config import settings
from app.schemas.gateway import GatewayMessage

class ContextManager:
    def __init__(
        self,
        budget_tokens: int = settings.CONTEXT_BUDGET_TOKENS,
        max_tool_result_tokens: int = settings.MAX_TOOL_RESULT_TOKENS,
    ):
        self.budget_tokens = budget_tokens
        self.max_tool_result_tokens = max_tool_result_tokens
        self._cache: Dict[str, str] = {}

    def estimate_tokens(self, text: str) -> int:
        """Conservative byte-based estimation (~3-4 chars per token)."""
        if not text:
            return 0
        return max(1, len(text.encode("utf-8")) // 3)

    def estimate_messages_tokens(self, messages: List[GatewayMessage]) -> int:
        total = 0
        for m in messages:
            total += 4  # overhead per message
            if m.content:
                total += self.estimate_tokens(m.content)
            if m.tool_calls:
                for tc in m.tool_calls:
                    total += self.estimate_tokens(tc.function.name) + self.estimate_tokens(tc.function.arguments)
        return total

    def cache_key(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        serialized = json.dumps({"t": tool_name, "a": arguments}, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get_cached_result(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[str]:
        key = self.cache_key(tool_name, arguments)
        return self._cache.get(key)

    def cache_result(self, tool_name: str, arguments: Dict[str, Any], result: str):
        key = self.cache_key(tool_name, arguments)
        self._cache[key] = result

    def prune_and_compress(self, messages: List[GatewayMessage]) -> Tuple[List[GatewayMessage], bool]:
        """
        Compresses older tool results if total messages exceed context budget.
        Maintains correspondence between tool_calls and tool results.
        """
        current_tokens = self.estimate_messages_tokens(messages)
        if current_tokens <= self.budget_tokens:
            return messages, False

        trimmed = False
        compressed_msgs: List[GatewayMessage] = []
        
        # Keep the latest messages intact, compress older tool messages
        for i, m in enumerate(messages):
            if m.role == "tool" and i < len(messages) - 1:
                # Compress older tool output if lengthy
                tokens = self.estimate_tokens(m.content or "")
                if tokens > 50:
                    compressed_content = (m.content or "")[:100] + "...[이전 도구 실행 결과 요약됨]"
                    compressed_msgs.append(
                        GatewayMessage(
                            role="tool",
                            content=compressed_content,
                            tool_call_id=m.tool_call_id,
                            name=m.name,
                        )
                    )
                    trimmed = True
                    continue
            compressed_msgs.append(m)

        # If still exceeding budget, safely remove older non-essential conversation pairs
        while self.estimate_messages_tokens(compressed_msgs) > self.budget_tokens and len(compressed_msgs) > 3:
            # Drop from index 1 (preserve system instruction at 0 if present)
            drop_idx = 1 if compressed_msgs[0].role == "system" else 0
            compressed_msgs.pop(drop_idx)
            trimmed = True

        return compressed_msgs, trimmed

context_manager = ContextManager()
