"""HTML page views. The floor plan itself is rendered client-side from the API."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie


@never_cache
@ensure_csrf_cookie
def public_floorplan(request):
    """Read-only floor plan, open to everyone on the network."""
    return render(request, 'floorplan/public.html', {
        'is_admin': request.user.is_authenticated and request.user.is_staff,
        'username': (request.user.get_username()
                     if request.user.is_authenticated else ''),
    })


@never_cache
@ensure_csrf_cookie
def admin_login(request):
    """Login form. Redirects home if the user is already signed in as staff."""
    if request.user.is_authenticated and request.user.is_staff:
        return redirect('admin_board')
    return render(request, 'floorplan/login.html')


@never_cache
@login_required
@ensure_csrf_cookie
def admin_board(request):
    """Drag-and-drop reassignment interface."""
    if not request.user.is_staff:
        return render(request, 'floorplan/forbidden.html', status=403)
    return render(request, 'floorplan/admin.html', {
        'username': request.user.get_username(),
    })
