"""QQ Bot channel package.

Public symbols remain importable from ``moss_gateway.channels.qq`` for
backward compatibility with the previous single-file implementation.
"""

from moss_gateway.channels.qq.channel import QQChannel, QQConfig
from moss_gateway.channels.qq.login_qr import (
    QQBotQRCodeResponse,
    QQBotQRCredentials,
    QQBotQRLogin,
    QQBotQRPollResult,
    QQBotQRWaitResult,
)

__all__ = [
    "QQBotQRCodeResponse",
    "QQBotQRCredentials",
    "QQBotQRLogin",
    "QQBotQRPollResult",
    "QQBotQRWaitResult",
    "QQChannel",
    "QQConfig",
]
