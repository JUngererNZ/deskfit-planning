"""WebSocket consumer broadcasting floor-plan changes to all clients.

The public view connects here to receive live updates. If the socket drops the
frontend falls back to polling, so this is an enhancement rather than a
hard dependency.
"""

import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer

from .services import FLOORPLAN_GROUP, build_snapshot

logger = logging.getLogger(__name__)


class FloorPlanConsumer(AsyncWebsocketConsumer):
    """Pushes the full plan snapshot on connect and after every change.

    A full snapshot is sent rather than a delta: the plan is small (111 cells)
    and a stateless payload removes any chance of clients drifting out of sync.
    """

    async def connect(self):
        try:
            snapshot = await self._snapshot()
        except Exception:
            logger.exception('Failed to build snapshot on websocket connect')
            await self.close(code=1011)
            return

        await self.channel_layer.group_add(FLOORPLAN_GROUP, self.channel_name)
        await self.accept()
        await self._send('snapshot', snapshot)
        logger.debug('Client %s connected', self.channel_name)

    async def disconnect(self, code):
        await self.channel_layer.group_discard(FLOORPLAN_GROUP, self.channel_name)
        logger.debug('Client %s disconnected (%s)', self.channel_name, code)

    async def receive(self, text_data=None, bytes_data=None):
        """Accept a client ping; ignore anything else."""
        if not text_data:
            return
        try:
            message = json.loads(text_data)
        except ValueError:
            return
        if message.get('type') == 'ping':
            await self._send('pong', {'ok': True})

    async def floorplan_update(self, event):
        """Handler for messages sent to the floorplan group."""
        await self._send('update', {
            'reason': event.get('reason', 'update'),
            'actor': event.get('actor', ''),
            'snapshot': event.get('snapshot'),
        })

    async def _send(self, message_type, payload):
        await self.send(text_data=json.dumps({
            'type': message_type, 'payload': payload}))

    @staticmethod
    async def _snapshot():
        from asgiref.sync import sync_to_async

        return await sync_to_async(build_snapshot)()
