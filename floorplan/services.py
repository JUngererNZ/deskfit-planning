"""Floor-plan services: snapshot, move, and person updates.

Business rules enforced here (and surfaced to the API as stable error codes):

* A person can occupy at most one desk; a desk holds at most one person.
* Fixed areas (walls, doors, rooms) are never assignable.
* Moves are wrapped in a transaction with row-level locking so two admins
  moving people at the same moment cannot corrupt the plan.
* A client may supply the desk version it last saw; a mismatch means someone
  else changed the desk in the interim and the move is rejected.
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Prefetch

from .models import (
    Assignment, AssignmentEvent, Desk, DeskKind, Person,
)

logger = logging.getLogger(__name__)

FLOORPLAN_GROUP = 'floorplan'

# Stable, machine-readable error codes. The frontend maps these to messages.
ERROR_DESK_NOT_FOUND = 'DESK_NOT_FOUND'
ERROR_DESK_NOT_ASSIGNABLE = 'DESK_NOT_ASSIGNABLE'
ERROR_DESK_OCCUPIED = 'DESK_OCCUPIED'
ERROR_PERSON_NOT_FOUND = 'PERSON_NOT_FOUND'
ERROR_PERSON_INACTIVE = 'PERSON_INACTIVE'
ERROR_NAME_REQUIRED = 'NAME_REQUIRED'
ERROR_NAME_TOO_LONG = 'NAME_TOO_LONG'
ERROR_NAME_TAKEN = 'NAME_TAKEN'
ERROR_STALE_VERSION = 'STALE_VERSION'
ERROR_INVALID_REQUEST = 'INVALID_REQUEST'
ERROR_NO_CHANGE = 'NO_CHANGE'


class ServiceError(Exception):
    """Raised for expected, user-correctable problems.

    Carries a stable ``code`` plus a human-readable ``message``.
    """

    def __init__(self, code, message, status=400, field=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.field = field

    def to_dict(self):
        payload = {'code': self.code, 'message': self.message}
        if self.field:
            payload['field'] = self.field
        return payload


def build_snapshot():
    """Full floor-plan state for clients.

    Desks are prefetched with their occupant so the query count stays constant
    regardless of plan size.
    """
    desks = list(
        Desk.objects
        .select_related('assignment', 'assignment__person')
        .order_by('grid_row', 'grid_col'))

    people = list(
        Person.objects.filter(is_active=True)
        .select_related('assignment', 'assignment__desk')
        .order_by('name'))

    occupied = sum(1 for d in desks if hasattr(d, 'assignment'))
    assignable = sum(1 for d in desks if d.is_assignable)

    return {
        'desks': [d.serialize() for d in desks],
        'people': [p.serialize() for p in people],
        'stats': {
            'total_desks': len(desks),
            'assignable_desks': assignable,
            'occupied_desks': occupied,
            'empty_desks': assignable - occupied,
            'total_people': len(people),
            'unassigned_people': sum(
                1 for p in people if not hasattr(p, 'assignment')),
            'placeholders': sum(1 for p in people if p.is_placeholder),
        },
    }


def broadcast_snapshot(reason='update', actor=None):
    """Push the current plan to every connected client.

    Failures are logged but never raised: a broadcast problem must not roll
    back an assignment that was already committed.
    """
    try:
        layer = get_channel_layer()
        if layer is None:
            logger.warning('No channel layer configured; skipping broadcast')
            return False
        async_to_sync(layer.group_send)(
            FLOORPLAN_GROUP,
            {'type': 'floorplan.update',
             'reason': reason,
             'actor': actor or '',
             'snapshot': build_snapshot()},
        )
        return True
    except Exception:
        logger.exception('Failed to broadcast floor plan update')
        return False


@transaction.atomic
def move_person(person_id, target_desk_id, actor=None, expected_version=None,
                allow_swap=False):
    """Move a person to a desk, or unassign when ``target_desk_id`` is None."""
    person = Person.objects.select_for_update().filter(
        pk=person_id, is_active=True).first()
    if person is None:
        raise ServiceError(
            ERROR_PERSON_NOT_FOUND,
            'That person no longer exists or is inactive.', status=404)

    current = Assignment.objects.select_related('desk').filter(
        person=person).first()
    from_desk = current.desk if current else None

    if target_desk_id in (None, '', 0):
        if current is None:
            raise ServiceError(
                ERROR_NO_CHANGE, f'{person.name} is not currently seated.')
        current.delete()
        from_desk.version += 1
        from_desk.save(update_fields=['version'])
        AssignmentEvent.objects.create(
            action=AssignmentEvent.Action.UNASSIGN, person=person,
            from_desk=from_desk, to_desk=None, actor=actor,
            detail=f'Unassigned {person.name} from {from_desk.ref}')
        return {'person': person, 'desk': None, 'swapped_with': None}

    try:
        target_id = int(target_desk_id)
    except (TypeError, ValueError):
        raise ServiceError(
            ERROR_INVALID_REQUEST, 'Target desk must be a numeric id.',
            field='desk_id')

    # Lock the target row so concurrent moves to the same desk serialise.
    target = Desk.objects.select_for_update().filter(pk=target_id).first()
    if target is None:
        raise ServiceError(
            ERROR_DESK_NOT_FOUND,
            f'Desk {target_id} does not exist.', status=404, field='desk_id')
    if not target.is_assignable:
        raise ServiceError(
            ERROR_DESK_NOT_ASSIGNABLE,
            f'{target.ref} is a fixed area and cannot hold a person.',
            field='desk_id')
    if expected_version is not None and target.version != expected_version:
        raise ServiceError(
            ERROR_STALE_VERSION,
            f'{target.ref} was changed by someone else. '
            'Refresh to see the current state.',
            status=409, field='version')

    if current and current.desk_id == target.pk:
        raise ServiceError(
            ERROR_NO_CHANGE, f'{person.name} is already at {target.ref}.')

    occupant = Assignment.objects.select_related('person', 'desk').filter(
        desk=target).first()
    swapped_with = None

    if occupant is not None:
        if occupant.person_id == person.pk:
            raise ServiceError(
                ERROR_NO_CHANGE, f'{person.name} is already at {target.ref}.')
        if not allow_swap:
            raise ServiceError(
                ERROR_DESK_OCCUPIED,
                f'{target.ref} is already occupied by {occupant.person.name}.',
                status=409, field='desk_id')
        # Swap: release the mover's current seat first so the incoming occupant
        # can inherit it without colliding with the unique constraint on desk.
        swapped_with = occupant.person
        if current is not None:
            previous_desk = current.desk
            current.delete()
            occupant.desk = previous_desk
            occupant.save(update_fields=['desk', 'assigned_at'])
        else:
            # The mover had no seat, so the occupant is simply displaced.
            occupant.delete()
    elif current is not None:
        current.delete()

    Assignment.objects.create(
        person=person, desk=target, assigned_by=actor,
        name_at_assignment=person.name)

    if from_desk is not None and (swapped_with is None or
                                  from_desk.pk != target.pk):
        from_desk.version += 1
        from_desk.save(update_fields=['version'])
    target.version += 1
    target.save(update_fields=['version'])

    AssignmentEvent.objects.create(
        action=AssignmentEvent.Action.MOVE, person=person,
        from_desk=from_desk, to_desk=target, actor=actor,
        detail=(f'Moved {person.name} to {target.ref}'
                + (f' (swapped with {swapped_with.name})' if swapped_with else '')))

    return {'person': person, 'desk': target, 'swapped_with': swapped_with}


@transaction.atomic
def update_person(person_id, data, actor=None):
    """Rename a person and/or set department, role and employment status."""
    person = Person.objects.select_for_update().filter(pk=person_id).first()
    if person is None:
        raise ServiceError(
            ERROR_PERSON_NOT_FOUND, 'That person does not exist.', status=404)

    if not isinstance(data, dict):
        raise ServiceError(ERROR_INVALID_REQUEST, 'Request body must be an object.')

    changed = []
    old_name = person.name

    if 'name' in data:
        raw = data['name']
        if raw is None or not str(raw).strip():
            raise ServiceError(
                ERROR_NAME_REQUIRED, 'Name cannot be blank.', field='name')
        new_name = ' '.join(str(raw).split())
        if len(new_name) > 100:
            raise ServiceError(
                ERROR_NAME_TOO_LONG,
                'Name must be 100 characters or fewer.', field='name')
        clash = Person.objects.filter(name__iexact=new_name).exclude(
            pk=person.pk).first()
        if clash is not None:
            raise ServiceError(
                ERROR_NAME_TAKEN,
                f'Another person is already named "{new_name}".',
                status=409, field='name')
        if new_name != person.name:
            person.name = new_name
            # A real name means the placeholder flag no longer applies.
            if not new_name.upper().startswith('HUMAN') and new_name != 'PERSON':
                person.is_placeholder = False
            changed.append(f'name "{old_name}" -> "{new_name}"')

    field_map = {
        'department': 100,
        'role': 100,
    }
    for field_name, max_len in field_map.items():
        if field_name in data:
            value = data[field_name]
            if value is None:
                value = ''
            value = ' '.join(str(value).split())
            if len(value) > max_len:
                raise ServiceError(
                    ERROR_NAME_TOO_LONG,
                    f'{field_name.capitalize()} must be {max_len} characters '
                    'or fewer.', field=field_name)
            if value != getattr(person, field_name):
                setattr(person, field_name, value)
                changed.append(f'{field_name} -> "{value}"')

    if 'employment_status' in data:
        valid = {choice[0] for choice in
                 Person._meta.get_field('employment_status').choices}
        value = (data['employment_status'] or 'unknown').strip().lower()
        if value not in valid:
            raise ServiceError(
                ERROR_INVALID_REQUEST,
                f'employment_status must be one of: {", ".join(sorted(valid))}.',
                field='employment_status')
        if value != person.employment_status:
            person.employment_status = value
            changed.append(f'employment_status -> "{value}"')

    if not changed:
        return person, []

    person.save()
    AssignmentEvent.objects.create(
        action=(AssignmentEvent.Action.RENAME
                if any(c.startswith('name ') for c in changed)
                else AssignmentEvent.Action.UPDATE),
        person=person, actor=actor, detail='; '.join(changed))
    logger.info('Person %s updated by %s: %s', person.pk, actor, '; '.join(changed))
    return person, changed
