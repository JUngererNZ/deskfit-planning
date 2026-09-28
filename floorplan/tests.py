"""Tests for the floor-plan API, services, and importer.

Covers the edge cases called out in the spec: invalid desk targets, duplicate
assignments, fixed areas, missing users, and authentication failures.
"""

import json
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from floorplan import services
from floorplan.models import (
    Assignment, AssignmentEvent, Desk, DeskKind, EmploymentStatus, Person,
)


def seed_floorplan():
    """Small deterministic plan used across tests."""
    fixed = Desk.objects.create(grid_col=1, grid_row=1, kind=DeskKind.FIXED,
                                label='BACK WALL', is_assignable=False)
    occupied = Desk.objects.create(grid_col=1, grid_row=2)
    empty = Desk.objects.create(grid_col=1, grid_row=3)
    other = Desk.objects.create(grid_col=1, grid_row=4)
    alice = Person.objects.create(name='ALICE')
    bob = Person.objects.create(name='BOB')
    Assignment.objects.create(person=alice, desk=occupied, name_at_assignment='ALICE')
    return {'fixed': fixed, 'occupied': occupied, 'empty': empty,
            'other': other, 'alice': alice, 'bob': bob}


class ImporterTests(TestCase):
    """The spreadsheet parser must resolve the duplicates deterministically."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        self.tmp.close()
        self._write_sheet([
            ['A1', 'BACK WALL'],
            ['C2', 'DOOR'],
            ['D2', 'RILEY'],
            ['E2', 'HENRY'],
            ['H2', 'SPARE'],
            ['G2', 'PERSON'],
            ['D4', 'HUMAN02'],
            ['E4', 'HUMAN01'],
            ['A9', 'HUMAN01'],   # duplicate: must be freed, not seated
        ])

    def _write_sheet(self, cells):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        for ref, value in cells:
            ws[ref] = value
        wb.save(self.tmp.name)

    def test_parses_fixed_areas_and_people(self):
        from floorplan.importer import parse_workbook

        result = parse_workbook(self.tmp.name)
        # RILEY, HENRY, PERSON, HUMAN02, HUMAN01 (the A9 repeat is freed).
        self.assertEqual(len(result.people), 5)
        labels = {d.label for d in result.fixed_areas}
        self.assertEqual(labels, {'BACK WALL', 'DOOR'})

    def test_duplicate_name_freed_not_seated(self):
        from floorplan.importer import parse_workbook

        result = parse_workbook(self.tmp.name)
        self.assertEqual(len(result.freed_duplicates), 1)
        self.assertEqual(result.freed_duplicates[0]['name'], 'HUMAN01')
        # First occurrence (E4, read before A9) keeps the seat.
        self.assertEqual(result.freed_duplicates[0]['kept_at'], 'E4')
        seated = {(p.name, p.grid_col, p.grid_row) for p in result.people}
        self.assertIn(('HUMAN01', 5, 4), seated)
        self.assertNotIn(('HUMAN01', 1, 9), seated)

    def test_spare_is_empty_desk_not_a_person(self):
        from floorplan.importer import parse_workbook

        result = parse_workbook(self.tmp.name)
        self.assertNotIn('SPARE', {p.name for p in result.people})
        self.assertTrue(any(d.grid_col == 8 and d.grid_row == 2
                            for d in result.empty_desks))

    def test_placeholder_detection(self):
        from floorplan.importer import parse_workbook

        result = parse_workbook(self.tmp.name)
        flags = {p.name: p.is_placeholder for p in result.people}
        self.assertTrue(flags['HUMAN01'])
        self.assertTrue(flags['PERSON'])
        self.assertFalse(flags['RILEY'])


class MoveServiceTests(TestCase):
    def setUp(self):
        self.f = seed_floorplan()
        self.admin = User.objects.create_superuser('boss', 'b@example.com', 'pw')

    def test_move_to_empty_desk(self):
        result = services.move_person(
            self.f['alice'].pk, self.f['empty'].pk, actor=self.admin)
        self.assertEqual(result['desk'], self.f['empty'])
        self.assertEqual(Assignment.objects.get().desk, self.f['empty'])
        self.f['empty'].refresh_from_db()
        self.assertEqual(self.f['empty'].version, 1)

    def test_move_to_occupied_desk_rejected_without_swap(self):
        services.move_person(self.f['alice'].pk, self.f['empty'].pk)
        with self.assertRaises(services.ServiceError) as ctx:
            services.move_person(self.f['bob'].pk, self.f['empty'].pk)
        self.assertEqual(ctx.exception.code, services.ERROR_DESK_OCCUPIED)

    def test_swap_between_two_people(self):
        services.move_person(self.f['alice'].pk, self.f['empty'].pk)
        services.move_person(self.f['bob'].pk, self.f['other'].pk)
        services.move_person(
            self.f['alice'].pk, self.f['other'].pk, allow_swap=True)
        self.assertEqual(
            Assignment.objects.get(person=self.f['alice']).desk, self.f['other'])
        self.assertEqual(
            Assignment.objects.get(person=self.f['bob']).desk, self.f['empty'])

    def test_move_to_fixed_area_rejected(self):
        with self.assertRaises(services.ServiceError) as ctx:
            services.move_person(self.f['alice'].pk, self.f['fixed'].pk)
        self.assertEqual(ctx.exception.code, services.ERROR_DESK_NOT_ASSIGNABLE)

    def test_unknown_desk_rejected(self):
        with self.assertRaises(services.ServiceError) as ctx:
            services.move_person(self.f['alice'].pk, 99999)
        self.assertEqual(ctx.exception.code, services.ERROR_DESK_NOT_FOUND)

    def test_unknown_person_rejected(self):
        with self.assertRaises(services.ServiceError) as ctx:
            services.move_person(99999, self.f['empty'].pk)
        self.assertEqual(ctx.exception.code, services.ERROR_PERSON_NOT_FOUND)

    def test_stale_version_rejected(self):
        self.f['empty'].refresh_from_db()
        stale = self.f['empty'].version + 5
        with self.assertRaises(services.ServiceError) as ctx:
            services.move_person(
                self.f['alice'].pk, self.f['empty'].pk,
                expected_version=stale)
        self.assertEqual(ctx.exception.code, services.ERROR_STALE_VERSION)
        self.assertEqual(ctx.exception.status, 409)

    def test_unassign(self):
        result = services.move_person(self.f['alice'].pk, None)
        self.assertIsNone(result['desk'])
        self.assertFalse(Assignment.objects.filter(person=self.f['alice']).exists())

    def test_audit_events_recorded(self):
        services.move_person(self.f['alice'].pk, self.f['empty'].pk)
        event = AssignmentEvent.objects.latest('created_at')
        self.assertEqual(event.action, AssignmentEvent.Action.MOVE)
        self.assertEqual(event.person, self.f['alice'])
        self.assertEqual(event.to_desk, self.f['empty'])


class UpdatePersonTests(TestCase):
    def setUp(self):
        self.f = seed_floorplan()

    def test_rename_and_set_fields(self):
        person, changed = services.update_person(
            self.f['alice'].pk,
            {'name': 'Alice Smith', 'department': 'Sales', 'role': 'SW ENGG',
             'employment_status': 'perm'})
        person.refresh_from_db()
        self.assertEqual(person.name, 'Alice Smith')
        self.assertEqual(person.department, 'Sales')
        self.assertEqual(person.role, 'SW ENGG')
        self.assertEqual(person.employment_status, EmploymentStatus.PERM)
        self.assertTrue(changed)

    def test_rename_clears_placeholder_flag(self):
        person = Person.objects.create(name='HUMAN07', is_placeholder=True)
        services.update_person(person.pk, {'name': 'Jordan Lee'})
        person.refresh_from_db()
        self.assertFalse(person.is_placeholder)

    def test_blank_name_rejected(self):
        with self.assertRaises(services.ServiceError) as ctx:
            services.update_person(self.f['alice'].pk, {'name': '   '})
        self.assertEqual(ctx.exception.code, services.ERROR_NAME_REQUIRED)

    def test_duplicate_name_rejected_case_insensitively(self):
        Person.objects.create(name='Jordan Lee')
        with self.assertRaises(services.ServiceError) as ctx:
            services.update_person(self.f['alice'].pk, {'name': 'jordan lee'})
        self.assertEqual(ctx.exception.code, services.ERROR_NAME_TAKEN)
        self.assertEqual(ctx.exception.status, 409)

    def test_name_too_long_rejected(self):
        with self.assertRaises(services.ServiceError) as ctx:
            services.update_person(self.f['alice'].pk, {'name': 'x' * 101})
        self.assertEqual(ctx.exception.code, services.ERROR_NAME_TOO_LONG)

    def test_invalid_status_rejected(self):
        with self.assertRaises(services.ServiceError) as ctx:
            services.update_person(
                self.f['alice'].pk, {'employment_status': 'nonsense'})
        self.assertEqual(ctx.exception.code, services.ERROR_INVALID_REQUEST)

    def test_rename_preserves_assignment(self):
        services.update_person(self.f['alice'].pk, {'name': 'Alice Smith'})
        self.assertEqual(
            Assignment.objects.get(person=self.f['alice']).desk,
            self.f['occupied'])


class ApiTests(TestCase):
    def setUp(self):
        self.f = seed_floorplan()
        self.admin = User.objects.create_superuser('boss', 'b@example.com', 'pw')
        self.client = Client()

    def post(self, url, payload):
        return self.client.post(
            url, data=json.dumps(payload), content_type='application/json')

    def login(self):
        self.client.force_login(self.admin)

    # -- authentication ---------------------------------------------------

    def test_login_requires_credentials(self):
        response = self.post('/api/auth/login', {'username': '', 'password': ''})
        self.assertEqual(response.status_code, 400)

    def test_login_rejects_bad_password(self):
        response = self.post(
            '/api/auth/login', {'username': 'boss', 'password': 'wrong'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['error']['code'], 'INVALID_CREDENTIALS')

    def test_non_staff_cannot_admin(self):
        User.objects.create_user('joe', 'j@example.com', 'pw')
        response = self.post(
            '/api/auth/login', {'username': 'joe', 'password': 'pw'})
        self.assertEqual(response.status_code, 403)

    def test_login_succeeds_and_sets_session(self):
        response = self.post(
            '/api/auth/login', {'username': 'boss', 'password': 'pw'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['user']['is_superuser'])

    # -- authorisation ---------------------------------------------------

    def test_move_requires_authentication(self):
        response = self.post(
            '/api/assignments/move',
            {'person_id': self.f['alice'].pk, 'desk_id': self.f['empty'].pk})
        self.assertEqual(response.status_code, 401)

    def test_move_rejects_non_staff(self):
        User.objects.create_user('joe', 'j@example.com', 'pw')
        self.client.force_login(
            User.objects.get(username='joe'))
        response = self.post(
            '/api/assignments/move',
            {'person_id': self.f['alice'].pk, 'desk_id': self.f['empty'].pk})
        self.assertEqual(response.status_code, 403)

    # -- public read -----------------------------------------------------

    def test_floorplan_is_public(self):
        response = self.client.get('/api/floorplan')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('desks', data)
        self.assertIn('stats', data)
        self.assertEqual(data['stats']['occupied_desks'], 1)

    # -- move endpoint ---------------------------------------------------

    def test_move_success(self):
        self.login()
        response = self.post('/api/assignments/move', {
            'person_id': self.f['alice'].pk,
            'desk_id': self.f['empty'].pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['desk']['ref'], 'A3')

    def test_move_missing_person_id(self):
        self.login()
        response = self.post('/api/assignments/move', {'desk_id': 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()['error']['code'], services.ERROR_INVALID_REQUEST)

    def test_move_to_fixed_area_returns_clear_error(self):
        self.login()
        response = self.post('/api/assignments/move', {
            'person_id': self.f['alice'].pk,
            'desk_id': self.f['fixed'].pk})
        self.assertEqual(response.status_code, 400)
        body = response.json()['error']
        self.assertEqual(body['code'], 'DESK_NOT_ASSIGNABLE')
        self.assertIn('fixed area', body['message'].lower())

    def test_move_to_occupied_desk_returns_409(self):
        self.login()
        response = self.post('/api/assignments/move', {
            'person_id': self.f['bob'].pk,
            'desk_id': self.f['occupied'].pk})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()['error']['code'], services.ERROR_DESK_OCCUPIED)

    def test_malformed_json_handled(self):
        self.login()
        response = self.client.post(
            '/api/assignments/move', data='{not json',
            content_type='application/json')
        self.assertEqual(response.status_code, 400)

    def test_non_post_rejected(self):
        self.login()
        response = self.client.get('/api/assignments/move')
        self.assertEqual(response.status_code, 405)

    # -- people endpoint -------------------------------------------------

    def test_people_requires_admin(self):
        self.assertEqual(self.client.get('/api/people').status_code, 401)
        self.login()
        self.assertEqual(self.client.get('/api/people').status_code, 200)

    def test_update_person_via_api(self):
        self.login()
        response = self.post('/api/people/update', {
            'person_id': self.f['alice'].pk,
            'name': 'Alice Smith', 'role': 'Open DM'})
        self.assertEqual(response.status_code, 200)
        self.f['alice'].refresh_from_db()
        self.assertEqual(self.f['alice'].name, 'Alice Smith')

    def test_request_id_header_returned(self):
        response = self.client.get('/api/floorplan')
        self.assertIn('X-Request-ID', response)


class ConcurrencyTests(TransactionTestCase):
    """Row locking must stop two admins claiming the same desk."""

    def setUp(self):
        self.f = seed_floorplan()
        self.admin = User.objects.create_superuser('boss', 'b@example.com', 'pw')

    def test_second_move_to_same_desk_is_rejected(self):
        services.move_person(
            self.f['alice'].pk, self.f['empty'].pk, actor=self.admin)
        with self.assertRaises(services.ServiceError) as ctx:
            services.move_person(
                self.f['bob'].pk, self.f['empty'].pk, actor=self.admin)
        self.assertEqual(ctx.exception.code, services.ERROR_DESK_OCCUPIED)
        # Exactly one person holds the desk.
        self.assertEqual(
            Assignment.objects.filter(desk=self.f['empty']).count(), 1)

    def test_desk_version_increments_so_stale_writes_fail(self):
        self.f['empty'].refresh_from_db()
        before = self.f['empty'].version
        services.move_person(
            self.f['alice'].pk, self.f['empty'].pk, actor=self.admin)
        self.f['empty'].refresh_from_db()
        self.assertEqual(self.f['empty'].version, before + 1)
        with self.assertRaises(services.ServiceError) as ctx:
            services.move_person(
                self.f['bob'].pk, self.f['empty'].pk,
                actor=self.admin, expected_version=before)
        self.assertEqual(ctx.exception.code, services.ERROR_STALE_VERSION)


class SnapshotTests(TestCase):
    def test_snapshot_shape_and_stats(self):
        seed_floorplan()
        snap = services.build_snapshot()
        self.assertEqual(len(snap['desks']), 4)
        self.assertEqual(snap['stats']['total_desks'], 4)
        self.assertEqual(snap['stats']['assignable_desks'], 3)
        self.assertEqual(snap['stats']['occupied_desks'], 1)
        self.assertEqual(snap['stats']['empty_desks'], 2)
        # Fixed areas are included but never assignable.
        fixed = [d for d in snap['desks'] if d['kind'] == 'fixed']
        self.assertEqual(len(fixed), 1)
        self.assertFalse(fixed[0]['is_assignable'])

    def test_desk_ref_formatting(self):
        desk = Desk.objects.create(grid_col=4, grid_row=4)
        self.assertEqual(desk.ref, 'D4')
        self.assertEqual(Desk.objects.create(grid_col=9, grid_row=41).ref, 'I41')
