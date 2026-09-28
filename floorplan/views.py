"""HTTP API for the floor plan.

All responses are JSON. Errors use a consistent envelope so the frontend can
branch on ``error.code`` rather than parsing messages.
"""

import json
import logging

from django.contrib.auth import authenticate, login, logout
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from . import services
from .models import Assignment, Desk, Person

logger = logging.getLogger(__name__)


def _error(code, message, status=400, field=None, request_id=None):
    payload = {'error': {'code': code, 'message': message}}
    if field:
        payload['error']['field'] = field
    if request_id:
        payload['error']['request_id'] = request_id
    response = JsonResponse(payload, status=status)
    if request_id:
        response['X-Request-ID'] = request_id
    return response


def _service_error(exc, request):
    logger.info(
        'Service error %s from %s: %s',
        exc.code, getattr(request, 'user', None), exc.message)
    return _error(exc.code, exc.message, exc.status, exc.field,
                  getattr(request, 'request_id', None))


def _require_admin(request):
    """Return None if authorised, else a 401/403 response."""
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return _error(
            'UNAUTHENTICATED', 'Please sign in to make changes.', status=401,
            request_id=getattr(request, 'request_id', None))
    if not user.is_staff:
        return _error(
            'FORBIDDEN', 'Your account does not have admin access.', status=403,
            request_id=getattr(request, 'request_id', None))
    return None


def _read_json(request):
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise services.ServiceError(
            services.ERROR_INVALID_REQUEST, 'Request body is not valid JSON.')
    if not isinstance(data, dict):
        raise services.ServiceError(
            services.ERROR_INVALID_REQUEST, 'Request body must be a JSON object.')
    return data


@require_POST
def api_login(request):
    try:
        data = _read_json(request)
    except services.ServiceError as exc:
        return _service_error(exc, request)

    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username or not password:
        return _error(
            services.ERROR_INVALID_REQUEST,
            'Username and password are both required.', status=400,
            request_id=getattr(request, 'request_id', None))

    user = authenticate(request, username=username, password=password)
    if user is None:
        # Deliberately vague, and logged for the operator to review.
        logger.warning('Failed login attempt for %r from %s', username,
                       request.META.get('REMOTE_ADDR'))
        return _error(
            'INVALID_CREDENTIALS', 'Incorrect username or password.',
            status=401, request_id=getattr(request, 'request_id', None))
    if not user.is_staff:
        logger.warning('Non-staff user %r attempted admin login', username)
        return _error(
            'FORBIDDEN', 'This account does not have admin access.',
            status=403, request_id=getattr(request, 'request_id', None))

    login(request, user)
    logger.info('Admin %s signed in', username)
    return JsonResponse({
        'ok': True,
        'user': {'username': user.get_username(),
                 'is_staff': user.is_staff,
                 'is_superuser': user.is_superuser},
    })


@require_POST
def api_logout(request):
    if getattr(request, 'user', None) and request.user.is_authenticated:
        logger.info('Admin %s signed out', request.user.get_username())
    logout(request)
    return JsonResponse({'ok': True})


@require_GET
def api_session(request):
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return JsonResponse({
            'authenticated': True,
            'is_staff': user.is_staff,
            'username': user.get_username(),
        })
    return JsonResponse({'authenticated': False, 'is_staff': False})


@require_GET
def api_floorplan(request):
    """Public, unauthenticated read of the current plan."""
    try:
        return JsonResponse(services.build_snapshot())
    except Exception:
        logger.exception('Failed to build floor plan snapshot')
        return _error(
            'SERVER_ERROR', 'Could not load the seating plan.',
            status=500, request_id=getattr(request, 'request_id', None))


@require_GET
def api_people(request):
    denied = _require_admin(request)
    if denied:
        return denied
    people = Person.objects.filter(is_active=True).select_related(
        'assignment', 'assignment__desk').order_by('name')
    return JsonResponse({
        'people': [p.serialize() for p in people],
    })


@require_POST
def api_move(request):
    denied = _require_admin(request)
    if denied:
        return denied

    try:
        data = _read_json(request)
    except services.ServiceError as exc:
        return _service_error(exc, request)

    person_id = data.get('person_id')
    desk_id = data.get('desk_id')
    if person_id in (None, ''):
        return _error(
            services.ERROR_INVALID_REQUEST, 'person_id is required.',
            status=400, field='person_id',
            request_id=getattr(request, 'request_id', None))

    try:
        result = services.move_person(
            person_id=person_id,
            target_desk_id=desk_id,
            actor=request.user,
            expected_version=data.get('version'),
            allow_swap=bool(data.get('swap', False)),
        )
    except services.ServiceError as exc:
        return _service_error(exc, request)
    except Exception:
        logger.exception('Unexpected error during move')
        return _error(
            'SERVER_ERROR', 'Something went wrong moving that person.',
            status=500, request_id=getattr(request, 'request_id', None))

    services.broadcast_snapshot(
        reason='move', actor=request.user.get_username())

    return JsonResponse({
        'ok': True,
        'person': result['person'].serialize(),
        'desk': result['desk'].serialize() if result['desk'] else None,
        'swapped_with': (
            result['swapped_with'].serialize()
            if result['swapped_with'] else None),
    })


@require_POST
def api_update_person(request):
    denied = _require_admin(request)
    if denied:
        return denied

    try:
        data = _read_json(request)
    except services.ServiceError as exc:
        return _service_error(exc, request)

    person_id = data.get('person_id')
    if person_id in (None, ''):
        return _error(
            services.ERROR_INVALID_REQUEST, 'person_id is required.',
            status=400, field='person_id',
            request_id=getattr(request, 'request_id', None))

    payload = {k: v for k, v in data.items() if k != 'person_id'}

    try:
        person, changed = services.update_person(
            person_id, payload, actor=request.user)
    except services.ServiceError as exc:
        return _service_error(exc, request)
    except Exception:
        logger.exception('Unexpected error updating person %s', person_id)
        return _error(
            'SERVER_ERROR', 'Something went wrong saving those details.',
            status=500, request_id=getattr(request, 'request_id', None))

    if changed:
        services.broadcast_snapshot(
            reason='person_update', actor=request.user.get_username())

    return JsonResponse({
        'ok': True,
        'person': person.serialize(),
        'changed': changed,
    })


@require_GET
def api_audit(request):
    """Recent assignment events, for debugging and dispute resolution."""
    denied = _require_admin(request)
    if denied:
        return denied
    from .models import AssignmentEvent

    events = AssignmentEvent.objects.select_related(
        'person', 'from_desk', 'to_desk', 'actor')[:100]
    return JsonResponse({
        'events': [
            {
                'action': e.action,
                'person': e.person.name if e.person else None,
                'from_desk': e.from_desk.ref if e.from_desk else None,
                'to_desk': e.to_desk.ref if e.to_desk else None,
                'actor': e.actor.get_username() if e.actor else 'system',
                'detail': e.detail,
                'created_at': e.created_at.isoformat(),
            }
            for e in events
        ],
    })
