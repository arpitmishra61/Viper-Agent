import os

_cwd = os.getcwd()


def get_cwd() -> str:
    return _cwd


def set_cwd(path: str) -> None:
    global _cwd
    _cwd = path
