from typing import Protocol


class GroupProviderError(Exception):
    """A group-provider call failed. The message is stored as `last_error`, so it
    must never carry a credential or token."""


class ExternalMembersDisabled(GroupProviderError):
    """The group rejects members outside the organization. A group owner must
    enable "Allow external members" in Google Groups, then the team re-syncs."""


class GroupOperationPending(GroupProviderError):
    """A write may still complete. Keep its intent until its effect is observed.

    `operation_name` is diagnostic only: Cloud Identity does not expose an
    operations.get method. Transport failures may not return a name at all.
    """

    def __init__(self, message: str, operation_name: str | None = None) -> None:
        super().__init__(message)
        self.operation_name = operation_name


class GroupProvider(Protocol):
    """Managed mailing groups (Google Groups in production, a fake in tests)."""

    domain: str  # new groups are created as <team slug>@<domain>

    def create_group(self, email: str, display_name: str, description: str) -> str:
        """Create the group and return its resource name (`groups/{id}`).

        If `email` already names a group whose description equals `description`
        (a previous attempt that succeeded without being recorded), return that
        group instead. Raises GroupProviderError if the address belongs to
        anything else, so a retry never creates a duplicate or adopts a stranger.
        """
        ...

    def list_members(self, group_name: str) -> dict[str, str]:
        """Lowercased member email -> membership resource name, for plain MEMBER
        memberships only. Owners and managers are omitted and never touched."""
        ...

    def add_member(self, group_name: str, email: str) -> None:
        """Add `email` as a MEMBER. Already a member is success. Raises
        ExternalMembersDisabled when the group refuses outside members, and
        GroupOperationPending when the write may still complete. Other
        GroupProviderErrors indicate a rejected change."""
        ...

    def remove_member(self, membership_name: str) -> None:
        """Delete the membership. Already gone is success. Raises
        GroupOperationPending when the write may still complete. Other
        GroupProviderErrors indicate a rejected change."""
        ...
