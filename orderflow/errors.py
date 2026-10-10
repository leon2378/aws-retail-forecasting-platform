class OrderFlowError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


class Conflict(OrderFlowError):
    def __init__(self, code, message):
        super().__init__(409, code, message)


class NotFound(OrderFlowError):
    def __init__(self, message="The requested record does not exist."):
        super().__init__(404, "not_found", message)
