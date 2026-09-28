"""WebSocket URL routing."""

from django.urls import re_path

from .consumers import FloorPlanConsumer

websocket_urlpatterns = [
    re_path(r'^ws/floorplan/$', FloorPlanConsumer.as_asgi()),
]
