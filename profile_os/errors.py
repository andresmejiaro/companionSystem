class ProfileNotFound(Exception):
    def __init__(self, profile_id: str):
        self.profile_id = profile_id
        super().__init__(f"unknown profile: {profile_id!r}")


class MalformedMemoryEvent(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"malformed memory event: {reason}")


class MalformedRecord(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"malformed record: {reason}")


class MemoryEventNotFound(Exception):
    def __init__(self, profile_id: str, event_id: str):
        super().__init__(f"no memory event {event_id!r} for profile {profile_id!r}")


class DynStoreNotFound(Exception):
    def __init__(self, profile_id: str, name: str):
        super().__init__(f"no dynamic store {name!r} for profile {profile_id!r}")


class DynStoreConflict(Exception):
    """Operation not allowed in the store's current status."""
    def __init__(self, reason: str):
        super().__init__(reason)


class SchemaError(Exception):
    """Invalid schema definition, or a record that violates an approved schema."""
    def __init__(self, reason: str):
        super().__init__(reason)
