import socket
import sys
from pathlib import Path
import pytest
from unittest.mock import AsyncMock
sys.path.insert(0, str(Path(__file__).parent))
from support import load_bot, close_bot

@pytest.fixture(autouse=True)
def prohibit_live_network(monkeypatch):
    original = socket.socket.connect
    def blocked(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('Tests cannot contact live networks')
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', blocked)

@pytest.fixture
def bot():
    module = load_bot()
    # Existing payment regressions isolate the new optional publication boundary.
    # Dedicated tests below exercise its real implementation and SQL separately.
    module._publish_public_testimonial = AsyncMock(return_value='disabled')
    yield module
    close_bot(module)
