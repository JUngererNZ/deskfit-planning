"""Data model for the office floor plan.

The structural foundation is the desk grid taken from DEMO-SEATING-PLAN.xlsx
(columns A-I, rows 1-41). Fixed areas (walls, doors, rooms) occupy the same
coordinate space but are never assignable.
"""

from django.conf import settings
from django.core.validators import MinLengthValidator
from django.db import models
from django.utils import timezone


class EmploymentStatus(models.TextChoices):
    """Employment status options. Unassigned is the default for imported data."""

    UNKNOWN = 'unknown', 'Unknown'
    PERM = 'perm', 'Permanent'
    CONTRACT = 'contract', 'Contract'
    TEMP = 'temp', 'Temp'


class DeskKind(models.TextChoices):
    DESK = 'desk', 'Desk'
    FIXED = 'fixed', 'Fixed area'


class Desk(models.Model):
    """A single grid cell on the floor plan.

    A cell is either an assignable desk or a fixed area such as a wall, door or
    meeting room. Row/column are stored numerically so ordering and rendering
    are trivial; ``ref`` exposes the spreadsheet-style label (e.g. "D4").
    """

    grid_col = models.PositiveSmallIntegerField(
        help_text='1-based column index (A=1, B=2, ... I=9)')
    grid_row = models.PositiveSmallIntegerField(
        help_text='1-based row index')
    kind = models.CharField(
        max_length=10, choices=DeskKind.choices, default=DeskKind.DESK)
    label = models.CharField(
        max_length=64, blank=True,
        help_text='Display name, e.g. "BACK WALL" for fixed areas')
    is_assignable = models.BooleanField(
        default=True,
        help_text='False for fixed areas, which can never receive a person')

    # Incremented on every assignment change. Clients send the version they
    # last saw so a stale concurrent edit is rejected rather than silently
    # overwriting a newer change.
    version = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['grid_row', 'grid_col']
        constraints = [
            models.UniqueConstraint(
                fields=['grid_col', 'grid_row'], name='unique_desk_cell'),
        ]

    def __str__(self):
        return f'{self.ref} ({self.label or "Desk"})'

    @property
    def ref(self):
        """Spreadsheet-style reference, e.g. "D4"."""
        return f'{chr(ord("A") + self.grid_col - 1)}{self.grid_row}'

    @property
    def occupant(self):
        """Assigned person, or None. Uses the reverse one-to-one accessor."""
        return getattr(self, 'assignment', None)

    def is_free(self):
        return self.is_assignable and not hasattr(self, 'assignment')

    def serialize(self, include_person=True):
        data = {
            'id': self.pk,
            'ref': self.ref,
            'grid_col': self.grid_col,
            'grid_row': self.grid_row,
            'kind': self.kind,
            'label': self.label,
            'is_assignable': self.is_assignable,
            'version': self.version,
        }
        if include_person and hasattr(self, 'assignment'):
            data['occupant'] = self.assignment.person.serialize()
        else:
            data['occupant'] = None
        return data


class Person(models.Model):
    """An occupant.

    Names imported from the spreadsheet are placeholders (``HUMAN01`` etc.) and
    are flagged via ``is_placeholder`` so the UI can present them honestly until
    an admin supplies the real name.
    """

    name = models.CharField(
        max_length=100, unique=True, validators=[MinLengthValidator(1)])
    department = models.CharField(max_length=100, blank=True)
    role = models.CharField(max_length=100, blank=True)
    employment_status = models.CharField(
        max_length=10, choices=EmploymentStatus.choices,
        default=EmploymentStatus.UNKNOWN)
    is_placeholder = models.BooleanField(
        default=False,
        help_text='True for placeholder names awaiting real details')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def serialize(self):
        return {
            'id': self.pk,
            'name': self.name,
            'department': self.department,
            'role': self.role,
            'employment_status': self.employment_status,
            'is_placeholder': self.is_placeholder,
            'is_active': self.is_active,
        }


class Assignment(models.Model):
    """Links one person to one desk.

    Enforces a strict one-to-one mapping in both directions: a person occupies
    at most one desk, and a desk holds at most one person.
    """

    person = models.OneToOneField(
        Person, on_delete=models.CASCADE, related_name='assignment')
    desk = models.OneToOneField(
        Desk, on_delete=models.CASCADE, related_name='assignment')
    assigned_at = models.DateTimeField(auto_now=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        blank=True, related_name='desk_assignments')

    # Retained so the audit trail survives a later rename.
    name_at_assignment = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['desk__grid_row', 'desk__grid_col']

    def __str__(self):
        return f'{self.person.name} @ {self.desk.ref}'

    def save(self, *args, **kwargs):
        if not self.name_at_assignment:
            self.name_at_assignment = self.person.name
        super().save(*args, **kwargs)


class AssignmentEvent(models.Model):
    """Append-only audit log of every desk move and person edit.

    Not surfaced in the UI, but queryable for debugging and dispute resolution.
    """

    class Action(models.TextChoices):
        MOVE = 'move', 'Move'
        RENAME = 'rename', 'Rename'
        UPDATE = 'update', 'Update'
        UNASSIGN = 'unassign', 'Unassign'

    action = models.CharField(max_length=10, choices=Action.choices)
    person = models.ForeignKey(
        Person, on_delete=models.SET_NULL, null=True, related_name='events')
    from_desk = models.ForeignKey(
        Desk, on_delete=models.SET_NULL, null=True, related_name='+')
    to_desk = models.ForeignKey(
        Desk, on_delete=models.SET_NULL, null=True, related_name='+')
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='+')
    detail = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['-created_at'])]

    def __str__(self):
        return f'{self.action} {self.person} @ {self.created_at:%Y-%m-%d %H:%M}'
