"""URL configuration."""

from django.contrib import admin
from django.urls import path

from floorplan import pages, views

urlpatterns = [
    path('django-admin/', admin.site.urls),

    # Pages
    path('', pages.public_floorplan, name='public_floorplan'),
    path('admin-login/', pages.admin_login, name='admin_login'),
    path('admin-board/', pages.admin_board, name='admin_board'),

    # API
    path('api/auth/login', views.api_login, name='api_login'),
    path('api/auth/logout', views.api_logout, name='api_logout'),
    path('api/auth/session', views.api_session, name='api_session'),
    path('api/floorplan', views.api_floorplan, name='api_floorplan'),
    path('api/people', views.api_people, name='api_people'),
    path('api/people/update', views.api_update_person, name='api_update_person'),
    path('api/assignments/move', views.api_move, name='api_move'),
    path('api/audit', views.api_audit, name='api_audit'),
]
