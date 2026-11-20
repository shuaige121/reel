class ReelError(Exception):
    """Every expected failure is raised with this; the tool layer serialises it as {"ok": false, "error": {...}}."""

    def __init__(self, code, message, **detail):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.detail = code, message, detail

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


class GateFailed(ReelError):
    """The gate does not pass: the tool itself finished, but the result cannot count as success. The CLI prints ok:false + error + the full result and exits non-zero."""

    def __init__(self, code, message, result, **detail):
        super().__init__(code, message, **detail)
        self.result = result
