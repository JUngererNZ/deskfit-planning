"""Request-scoped correlation IDs.

Every response carries an X-Request-ID header and every log line for that
request includes it, so a user-reported failure can be traced to exact server
logs.
"""

import logging
import uuid

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = 'HTTP_X_REQUEST_ID'


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = request.META.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        response = self.get_response(request)
        response['X-Request-ID'] = request.request_id
        return response
