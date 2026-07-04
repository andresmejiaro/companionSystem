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
