"""WebSocket message handlers, split by message type (P0-2).

Public entry point: `websocket_handler` (connection lifecycle + dispatch).
"""
from app.ws.handlers.connection import websocket_handler

__all__ = ["websocket_handler"]
