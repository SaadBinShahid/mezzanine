"""
Drop-in replacement for ``django.conf.settings`` that provides a
consistent access method for settings defined in applications, the project
or Django itself. Settings can also be made editable via the admin.
"""

from __future__ import unicode_literals
from weakref import WeakKeyDictionary
from future.builtins import bytes, str

from functools import partial
from importlib import import_module
from warnings import warn

from django.conf import settings as django_settings
from django.utils.functional import Promise
from django.utils.module_loading import module_has_submodule

from mezzanine import __version__
from mezzanine.core.request import current_request

registry = {}


def register_setting(name=None, label=None, editable=False, description=None,
                     default=None, choices=None, append=False,
                     translatable=False):
    """
    Registers a setting that can be edited via the admin. This mostly
    equates to storing the given args as a dict in the ``registry``
    dict by name.
    """
    if name is None:
        raise TypeError("mezzanine.conf.register_setting requires the "
                        "'name' keyword argument.")
    if editable and default is None:
        raise TypeError("mezzanine.conf.register_setting requires the "
                        "'default' keyword argument when 'editable' is True.")

    # append is True when called from an app (typically external)
    # after the setting has already been registered, with the
    # intention of appending to its default value.
    if append and name in registry:
        registry[name]["default"] += default
        return

    # If an editable setting has a value defined in the
    # project's settings.py module, it can't be editable, since
    # these lead to a lot of confusion once its value gets
    # defined in the db.
    if hasattr(django_settings, name):
        editable = False
    if label is None:
        label = name.replace("_", " ").title()

    # Python 2/3 compatibility. isinstance() is overridden by future
    # on Python 2 to behave as Python 3 in conjunction with either
    # Python 2's native types or the future.builtins types.
    if isinstance(default, bool):
        # Prevent bools treated as ints
        setting_type = bool
    elif isinstance(default, int):
        # An int or long or subclass on Py2
        setting_type = int
    elif isinstance(default, (str, Promise)):
        # A unicode or subclass on Py2
        setting_type = str
    elif isinstance(default, bytes):
        # A byte-string or subclass on Py2
        setting_type = bytes
    else:
        setting_type = type(default)
    registry[name] = {"name": name, "label": label, "editable": editable,
                      "description": description, "default": default,
                      "choices": choices, "type": setting_type,
                      "translatable": translatable}


class Settings(object):
    """
    An object that provides settings via dynamic attribute access.

    Settings that are registered as editable will be stored in the
    database once the site settings form in the admin is first saved.
    When these values are accessed via this settings object, *all*
    database stored settings get retrieved from the database.

    When accessing uneditable settings their default values are used,
    unless they've been given a value in the project's settings.py
    module.

    The settings object also provides access to Django settings via
    ``django.conf.settings``, in order to provide a consistent method
    of access for all settings.
    """

    class Sentinel(object):
        """A Weakly-referable wrapper of 'object'."""
        pass

    NULL_REQUEST = Sentinel()

    # These functions map setting types to the functions that should be
    # used to convert them from the Unicode string stored in the database.
    # If a type doesn't appear in this map, the type itself will be used.
    TYPE_FUNCTIONS = {
        bool: lambda val: val != "False",
        bytes: partial(bytes, encoding='utf8')
    }

    def __init__(self):
        """
        The ``_editable_caches`` attribute maps Request objects to dicts of
        editable settings loaded from the database. We cache settings per-
        request to ensure that the database is hit at most once per request,
        and that each request sees the same settings for its duration.
        """
        self._editable_caches = WeakKeyDictionary({self.NULL_REQUEST: {}})

    @property
    def _current_request(self):
        return current_request() or self.NULL_REQUEST

    def use_editable(self):
        """
        Clear the cache for the current request so that editable settings are
        fetched from the database on next access. Using editable settings
        is the default, so this is deprecated in favour of ``clear_cache()``.
        """
        self.clear_cache()
        warn("Settings.use_editable() is deprecated in favour of "
             "Settings.clear_cache(). Please update your code.",
             DeprecationWarning,
             stacklevel=2)

    def clear_cache(self):
        """Clear the settings cache for a given request (or no request)."""
        self._editable_caches.pop(self._current_request, None)

    def _get_editable(self, request):
        """
        Get the dictionary of editable settings for a given request. Settings
        are fetched from the database once per request and then stored in
        ``_editable_caches``, a WeakKeyDictionary that will automatically
        discard each entry when no more references to the request exist.
        """
        try:
            editable_settings = self._editable_caches[request]
        except KeyError:
            editable_settings = self._editable_caches[request] = self._load()
        return editable_settings

    def _load(self):
        """
        Load editable settings from the database and return them as a dict.
        Delete any settings from the database that are no longer registered,
        and emit a warning if there are settings that are defined in both
        settings.py and the database.
        """
        from mezzanine.conf.models import Setting

        removed_settings = []
        conflicting_settings = []
        new_cache = {}

        for setting_obj in Setting.objects.all():
            try:
                registry[setting_obj.name]
            except KeyError:
                # Setting in DB isn't registered (removed from code),
                # so add to removal list and skip remaining handling.
                removed_settings.append(setting_obj.name)
                continue

            # Convert DB value to correct type.
            setting_type = registry[setting_obj.name]["type"]
            type_fn = self.TYPE_FUNCTIONS.get(setting_type, setting_type)
            try:
                setting_value = type_fn(setting_obj.value)
            except ValueError:
                # Shouldn't occur, but just a safeguard
                # for if the db value somehow ended up as
                # an invalid type.
                setting_value = registry[setting_obj.name]["default"]

            # Only use DB setting if it's not defined in settings.py
            # module, in which case add it to conflicting list for
            # warning.
            try:
                getattr(django_settings, setting_obj.name)
            except AttributeError:
                new_cache[setting_obj.name] = setting_value
            else:
                if setting_value != registry[setting_obj.name]["default"]:
                    conflicting_settings.append(setting_obj.name)

        if removed_settings:
            Setting.objects.filter(name__in=removed_settings).delete()
        if conflicting_settings:
            warn("These settings are defined in both settings.py and "
                 "the database: %s. The settings.py values will be used."
                 % ", ".join(conflicting_settings))

        return new_cache

    def __getattr__(self, name):

        # Lookup name as a registered setting or a Django setting.
        try:
            setting = registry[name]
        except KeyError:
            return getattr(django_settings, name)

        # If the setting isn't editable, just return it
        if not setting["editable"]:
            return getattr(django_settings, name, setting["default"])

        # Use cached editable setting if found, otherwise use the
        # value defined in the project's settings.py module if it
        # exists, finally falling back to the default defined when
        # registered.
        editable_cache = self._get_editable(request=self._current_request)
        try:
            return editable_cache[name]
        except KeyError:
            return getattr(django_settings, name, setting["default"])


mezz_first = lambda app: not app.startswith("mezzanine.")
for app in sorted(django_settings.INSTALLED_APPS, key=mezz_first):
    try:
        module = import_module(app)
    except ImportError:
        pass
    else:
        try:
            import_module("%s.defaults" % app)
        except:
            if module_has_submodule(module, "defaults"):
                raise

settings = Settings()
