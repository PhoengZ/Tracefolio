class TracefolioError(Exception):
    """Only logical, non-sensitive messages may cross the CLI boundary."""

    def __init__(self, code: str, safe_message: str = "Operation could not be completed.",
                 exit_code: int = 2):
        self.code = code
        self.safe_message = safe_message
        self.exit_code = exit_code
        super().__init__(f"{code}: {safe_message}")

