"""Fast-path handlers that never touch the database: move, cancel_queue."""
from app.ws.manager import manager
from app.ws.handlers.context import ConnectionContext


async def handle_cancel_queue(ctx: ConnectionContext, data: dict) -> None:
    """Remove the user from any resident chat queue they are waiting in."""
    for _rid, queue in list(manager.chat_queue.items()):
        if ctx.user_id in queue:
            queue.remove(ctx.user_id)


async def handle_move(ctx: ConnectionContext, data: dict) -> None:
    x = float(data.get("x", 0))
    y = float(data.get("y", 0))
    direction = str(data.get("direction", "down"))
    manager.update_position(ctx.user_id, x, y, direction, ctx.user_name)
    await manager.broadcast(
        {"type": "player_moved", "player_id": ctx.user_id,
         "name": ctx.user_name,
         "x": x, "y": y, "direction": direction},
        exclude=ctx.user_id,
    )
