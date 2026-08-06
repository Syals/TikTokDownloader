"""poc/explore 共享测试 fixture。"""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def project_tmp():
    """项目内临时目录，避开系统 %TEMP% 可能无写权限的问题。"""
    with tempfile.TemporaryDirectory(dir=".") as tmp:
        yield Path(tmp)
