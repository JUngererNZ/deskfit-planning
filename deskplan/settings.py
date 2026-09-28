"""
Django settings for the deskplan project.

Configuration is environment-driven so the same code runs locally and on an
internal server. See README.md for the full list of environment variables.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def env_list(name, default=''):
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(',') if item.strip()]


# --------------------------------------------------------------------------
# Core security
# --------------------------------------------------------------------------

# A real SECRET_KEY must be supplied in production; the fallback exists only so
# the prototype boots with zero configuration. Startup logs a warning if used.
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-pbforo_9disl(uea&=3*i3c!pponnzp=haa)m0he3&*8bh(uyv',
)
USING_DEFAULT_SECRET_KEY = SECRET_KEY.startswith('django-insecure-')

DEBUG = env_bool('DJANGO_DEBUG', True)

ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,[::1]')
if not DEBUG and ALLOWED_HOSTS == ['localhost', '127.0.0.1', '[::1]']:
    # Running with DEBUG off but no hosts configured is almost always a mistake.
    ALLOWED_HOSTS = ['*']


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'floorplan',
    'whitenoise.runserver_nostatic',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'floorplan.middleware.RequestIDMiddleware',
]

ROOT_URLCONF = 'deskplan.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'deskplan.wsgi.application'
ASGI_APPLICATION = 'deskplan.asgi.application'


# --------------------------------------------------------------------------
# Database - SQLite by default, PostgreSQL via DATABASE_URL
# --------------------------------------------------------------------------

DATABASE_URL = os.environ.get('DATABASE_URL', '')

if DATABASE_URL:
    from urllib.parse import urlparse, unquote

    parsed = urlparse(DATABASE_URL)
    if parsed.scheme in ('postgres', 'postgresql', 'psql'):
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': unquote(parsed.path.lstrip('/')),
                'USER': unquote(parsed.username or ''),
                'PASSWORD': unquote(parsed.password or ''),
                'HOST': parsed.hostname or 'localhost',
                'PORT': str(parsed.port or 5432),
                'CONN_MAX_AGE': 60,
            }
        }
    elif parsed.scheme == 'sqlite':
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': unquote(parsed.path or str(BASE_DIR / 'db.sqlite3')),
            }
        }
    else:
        raise ValueError(f'Unsupported DATABASE_URL scheme: {parsed.scheme}')
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': {'timeout': 20},
        }
    }


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = '/admin-login/'

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
# Secure cookies are on by default whenever DEBUG is off; override only if you
# terminate TLS somewhere Django cannot see.
SESSION_COOKIE_SECURE = env_bool('DJANGO_SECURE_COOKIES', not DEBUG)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS')

# Idle timeout so a forgotten admin session does not stay live all day.
SESSION_COOKIE_AGE = int(os.environ.get('DJANGO_SESSION_AGE_SECONDS', 8 * 60 * 60))
SESSION_SAVE_EVERY_REQUEST = True


# --------------------------------------------------------------------------
# Internationalisation
# --------------------------------------------------------------------------

LANGUAGE_CODE = 'en-us'
TIME_ZONE = os.environ.get('DJANGO_TIME_ZONE', 'UTC')
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

# Daphne does not serve static files, so WhiteNoise handles them. It also
# keeps the app working when moved behind a reverse proxy in production.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {
        'BACKEND': (
            # No manifest in development, so edits show up without
            # re-running collectstatic after every change.
            'whitenoise.storage.CompressedStaticFilesStorage' if DEBUG
            else 'whitenoise.storage.CompressedManifestStaticFilesStorage'
        ),
    },
}


# --------------------------------------------------------------------------
# Channels - real-time floor plan updates
# --------------------------------------------------------------------------

REDIS_URL = os.environ.get('REDIS_URL', '')

if REDIS_URL:
    # Required for multi-process/multi-worker production deployments.
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [REDIS_URL]},
        }
    }
else:
    # Single-process prototype. Does NOT broadcast across multiple workers.
    CHANNEL_LAYERS = {
        'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}
    }


# --------------------------------------------------------------------------
# Security hardening (active when DEBUG is off)
# --------------------------------------------------------------------------

if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool('DJANGO_SSL_REDIRECT', True)
    SECURE_HSTS_SECONDS = int(os.environ.get('DJANGO_HSTS_SECONDS', 31536000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'


# --------------------------------------------------------------------------
# Logging - errors always go to file for debugging
# --------------------------------------------------------------------------

LOG_DIR = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOG_DIR / 'deskplan.log',
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
    },
    'loggers': {
        'django.db.backends': {
            'handlers': ['console', 'file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'floorplan': {
            'handlers': ['console', 'file'],
            'level': os.environ.get('DJANGO_LOG_LEVEL', 'INFO'),
            'propagate': False,
        },
    },
}

# This app sends no email. The console backend is kept deliberately so no
# message can ever be sent to real recipients by accident; set EMAIL_BACKEND
# if you add password-reset or invite emails later.
MAILERS = {
    'default': {'BACKEND': os.environ.get(
        'EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')},
}
