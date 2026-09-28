"""ASGI entrypoint wiring HTTP and WebSocket traffic.

Run with Daphne (required for WebSocket support):

    .venv/bin/daphne -b 0.0.0.0 -p 8000 deskplan.asgi:application
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'deskplan.settings')

# Initialise Django before importing anything that touches models.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from floorplan.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(
        URLRouter(websocket_urlpatterns)),
})
