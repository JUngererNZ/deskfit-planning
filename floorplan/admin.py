from django.contrib import admin

from .models import Assignment, AssignmentEvent, Desk, Person


@admin.register(Desk)
class DeskAdmin(admin.ModelAdmin):
    list_display = ['ref', 'kind', 'label', 'is_assignable', 'version']
    list_filter = ['kind', 'is_assignable']
    search_fields = ['label']


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ['name', 'department', 'role', 'employment_status',
                    'is_placeholder', 'is_active', 'seat']
    list_filter = ['is_placeholder', 'is_active', 'employment_status']
    search_fields = ['name', 'department', 'role']

    @admin.display(description='Desk')
    def seat(self, obj):
        assignment = getattr(obj, 'assignment', None)
        return assignment.desk.ref if assignment else '--'


@admin.register(Assignment)
class AssignmentAdmin(admin.ModelAdmin):
    list_display = ['person', 'desk', 'assigned_at', 'assigned_by']
    search_fields = ['person__name']


@admin.register(AssignmentEvent)
class AssignmentEventAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'action', 'person', 'from_desk', 'to_desk',
                    'actor', 'detail']
    list_filter = ['action']
    readonly_fields = [f.name for f in AssignmentEvent._meta.fields]
