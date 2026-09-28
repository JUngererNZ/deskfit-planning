"""Import the seating plan spreadsheet into the database.

Idempotent: running it repeatedly converges on the same state rather than
creating duplicates, so it is safe to re-run after adding people by hand.
Existing manual desk moves are preserved unless --reset is passed.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from floorplan import grid as grid_const
from floorplan.importer import parse_workbook
from floorplan.models import (
    Assignment, AssignmentEvent, Desk, DeskKind, Person,
)

logger = logging.getLogger(__name__)

DEFAULT_SPREADSHEET = (
    Path(settings.BASE_DIR) / 'DEMO-SEATING-PLAN.xlsx')


class Command(BaseCommand):
    help = 'Import desks, fixed areas and occupants from the seating spreadsheet.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--file', default=str(DEFAULT_SPREADSHEET),
            help='Path to the seating plan .xlsx file')
        parser.add_argument(
            '--reset', action='store_true',
            help='Delete existing desks, people and assignments first')

    @transaction.atomic
    def handle(self, *args, **options):
        source = Path(options['file'])
        if not source.is_file():
            raise CommandError(f'Spreadsheet not found: {source}')

        if options['reset']:
            self.stdout.write('Resetting existing floor plan data...')
            AssignmentEvent.objects.all().delete()
            Assignment.objects.all().delete()
            Person.objects.all().delete()
            Desk.objects.all().delete()

        try:
            parsed = parse_workbook(source)
        except Exception as exc:
            logger.exception('Failed to parse spreadsheet %s', source)
            raise CommandError(f'Could not read spreadsheet: {exc}') from exc

        desks = self._sync_desks(parsed)
        people = self._sync_people(parsed)
        assignments, skipped = self._sync_assignments(parsed.people)

        self._add_supplementary_fixed_areas(desks)

        self.stdout.write(self.style.SUCCESS(
            f'Imported from {source.name}:'))
        self.stdout.write(f'  desks          : {Desk.objects.count()}')
        self.stdout.write(f'  fixed areas    : '
                          f'{Desk.objects.filter(kind=DeskKind.FIXED).count()}')
        self.stdout.write(f'  people         : {Person.objects.count()}')
        self.stdout.write(f'  assigned       : {Assignment.objects.count()}')
        self.stdout.write(f'  unassigned     : '
                          f'{Person.objects.filter(assignment__isnull=True).count()}')
        self.stdout.write(f'  placeholders   : '
                          f'{Person.objects.filter(is_placeholder=True).count()}')
        self.stdout.write(f'  empty desks    : '
                          f'{sum(1 for d in Desk.objects.all() if d.is_free())}')
        if skipped:
            self.stdout.write(f'  not seated     : {len(skipped)}')
            for name, reason in skipped[:10]:
                self.stdout.write(f'      {name}: {reason}')

        dupes = parsed.freed_duplicates
        if dupes:
            self.stdout.write(self.style.WARNING(
                f'  freed {len(dupes)} duplicate cells '
                f'({len({d["name"] for d in dupes})} repeated names); '
                'first occurrence kept its seat.'))
        for warning in parsed.warnings:
            self.stdout.write(self.style.WARNING(f'  warning: {warning}'))

        AssignmentEvent.objects.create(
            action=AssignmentEvent.Action.UPDATE,
            actor=None,
            detail=(f'Imported {len(desks)} desks / {len(people)} people '
                    f'from {source.name}'))
        logger.info('Import complete: %d desks, %d people, %d assignments',
                    len(desks), len(people), len(assignments))

    def _sync_desks(self, parsed):
        desks = {}
        for item in parsed.desks:
            desk, _ = Desk.objects.get_or_create(
                grid_col=item.grid_col,
                grid_row=item.grid_row,
                defaults={
                    'kind': item.kind,
                    'label': item.label,
                    'is_assignable': item.is_assignable,
                })
            if desk.kind != item.kind:
                desk.kind = item.kind
                desk.label = item.label
                desk.is_assignable = item.is_assignable
                desk.save(update_fields=['kind', 'label', 'is_assignable'])
            desks[(item.grid_col, item.grid_row)] = desk
        return desks

    def _sync_people(self, parsed):
        people = {}
        for item in parsed.people:
            person, _ = Person.objects.get_or_create(
                name=item.name,
                defaults={'is_placeholder': item.is_placeholder})
            # Never downgrade a name an admin has already fleshed out.
            if item.is_placeholder and not person.is_placeholder:
                pass
            elif not item.is_placeholder and person.is_placeholder:
                person.is_placeholder = False
                person.save(update_fields=['is_placeholder'])
            people[item.name] = person
        return people

    def _sync_assignments(self, parsed_people):
        """Seat each person, tolerating desks already claimed by a manual move."""
        assignments = []
        skipped = []

        for item in parsed_people:
            person = Person.objects.filter(name=item.name).first()
            if person is None:
                skipped.append((item.name, 'person record missing'))
                continue

            desk = Desk.objects.filter(
                grid_col=item.grid_col, grid_row=item.grid_row).first()
            if desk is None:
                skipped.append((item.name, f'desk {item.grid_col}/'
                                           f'{item.grid_row} missing'))
                continue
            if not desk.is_assignable:
                skipped.append((item.name, 'target is a fixed area'))
                continue

            occupied = Assignment.objects.filter(desk=desk).select_related(
                'person').first()
            if occupied is not None:
                if occupied.person_id == person.pk:
                    assignments.append(occupied)
                    continue
                # Another person already holds this desk (manual move). Leave
                # that assignment alone and keep our person unassigned.
                skipped.append(
                    (item.name, f'desk {desk.ref} held by {occupied.person.name}'))
                continue

            if hasattr(person, 'assignment'):
                # Person already seated elsewhere; respect the existing move.
                skipped.append(
                    (item.name, f'already seated at '
                                f'{person.assignment.desk.ref}'))
                continue

            assignment = Assignment.objects.create(
                person=person, desk=desk, name_at_assignment=person.name)
            assignments.append(assignment)
            desk.version += 1
            desk.save(update_fields=['version'])

        return assignments, skipped

    def _add_supplementary_fixed_areas(self, desks):
        """Reserve the grid cells used by the four areas missing from the sheet.

        Only applied when those cells are still unoccupied, so a later manual
        assignment is never silently destroyed.
        """
        for start_col, end_col, start_row, end_row, label in \
                grid_const.SUPPLEMENTARY_FIXED_AREAS:
            for r in range(start_row, end_row + 1):
                for c in range(start_col, end_col + 1):
                    if c > grid_const.GRID_COLUMNS or r > grid_const.GRID_ROWS:
                        continue
                    desk = desks.get((c, r)) or Desk.objects.filter(
                        grid_col=c, grid_row=r).first()
                    if desk is None:
                        desk = Desk.objects.create(
                            grid_col=c, grid_row=r, kind=DeskKind.FIXED,
                            label=label, is_assignable=False)
                    elif desk.is_assignable and not hasattr(desk, 'assignment'):
                        desk.kind = DeskKind.FIXED
                        desk.label = label
                        desk.is_assignable = False
                        desk.save(
                            update_fields=['kind', 'label', 'is_assignable'])
                    # If the cell is already occupied, leave it alone.
