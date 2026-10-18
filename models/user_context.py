"""
UserContext - Session metadata container for stateless orchestration.

This dataclass stores session-level information that allows the orchestrator
to remain stateless while maintaining user preferences and history.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class UserContext:
    """
    Immutable container for user session metadata.

    Stores user preferences, session state, and conversation history
    to enable stateless orchestration.

    Attributes:
        session_id: Unique identifier for this session
        user_id: Optional user identifier for multi-user systems
        preferences: User preferences (e.g., temperature, max_tokens)
        metadata: Additional session metadata (e.g., region, client_version)
        conversation_history: List of conversation messages
        created_at: Session creation timestamp
    """

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str | None = None
    preferences: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)

    def add_message(self, role: str, content: str) -> "UserContext":
        """
        Create a new UserContext with an added message.

        Args:
            role: Message role ('user', 'assistant', 'system')
            content: Message content

        Returns:
            New UserContext instance with updated conversation history
        """
        new_history = self.conversation_history.copy()
        new_history.append({"role": role, "content": content})

        return UserContext(
            session_id=self.session_id,
            user_id=self.user_id,
            preferences=self.preferences.copy(),
            metadata=self.metadata.copy(),
            conversation_history=new_history,
            created_at=self.created_at,
        )

    def clear_history(self, keep_system: bool = True) -> "UserContext":
        """
        Create a new UserContext with cleared conversation history.

        Args:
            keep_system: If True, preserve system messages

        Returns:
            New UserContext instance with cleared history
        """
        new_history = []
        if keep_system:
            new_history = [msg for msg in self.conversation_history if msg.get("role") == "system"]

        return UserContext(
            session_id=self.session_id,
            user_id=self.user_id,
            preferences=self.preferences.copy(),
            metadata=self.metadata.copy(),
            conversation_history=new_history,
            created_at=self.created_at,
        )

    def get_messages(self) -> list[dict[str, Any]]:
        """
        Get conversation messages in API format.

        Returns:
            List of message dictionaries
        """
        return self.conversation_history.copy()

    def get_message_count(self) -> int:
        """
        Get the number of messages in conversation history.

        Returns:
            Message count
        """
        return len(self.conversation_history)
