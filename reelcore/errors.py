class ReelError(Exception):
    """所有可预期失败都用它抛出；工具层把它序列化成 {"ok": false, "error": {...}}。"""

    def __init__(self, code, message, **detail):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.detail = code, message, detail

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


class GateFailed(ReelError):
    """闸门不放行：工具本身跑完了，但结果不能算成功。CLI 输出 ok:false + error + 完整 result，退出码非零。"""

    def __init__(self, code, message, result, **detail):
        super().__init__(code, message, **detail)
        self.result = result
