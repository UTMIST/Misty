"""Keep OAuth codes/state out of Uvicorn's standard access log."""

import logging


class GitHubCallbackLogFilter(logging.Filter):
    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = args[2]
            if path.split("?", 1)[0].endswith("/github/oauth/callback"):
                record.args = (*args[:2], path.split("?", 1)[0], *args[3:])
        return True


def protect_callback_logs():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, GitHubCallbackLogFilter) for f in logger.filters):
        logger.addFilter(GitHubCallbackLogFilter())
