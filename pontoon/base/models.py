import hashlib
import json
import logging
import math
import operator
import os.path
import re
import requests

import Levenshtein

from collections import defaultdict
from dirtyfields import DirtyFieldsMixin
from django.db.models.functions import Length, Substr, Cast
from functools import reduce
from urllib.parse import quote, urlencode, urlparse

from django.conf import settings
from django.contrib.auth.models import User, Group
from django.contrib.postgres.fields import ArrayField

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.db.models import (
    Count,
    F,
    Prefetch,
    Q,
    Sum,
    Case,
    When,
    Value,
    ExpressionWrapper,
)
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property

from guardian.shortcuts import get_objects_for_user
from jsonfield import JSONField

from pontoon.actionlog.models import ActionLog
from pontoon.actionlog.utils import log_action
from pontoon.base import utils
from pontoon.base.fluent import get_simple_preview
from pontoon.checks import DB_FORMATS
from pontoon.checks.utils import save_failed_checks
from pontoon.db import IContainsCollate, LevenshteinDistance  # noqa
from pontoon.sync import KEY_SEPARATOR

log = logging.getLogger(__name__)


def combine_entity_filters(entities, filter_choices, filters, *args):
    """Return a combination of filters to apply to an Entity object.

    The content for each filter is defined in the EntityQuerySet helper class, using methods
    that have the same name as the filter. Each subset of filters is combined with the others
    with the OR operator.

    :arg EntityQuerySet entities: an Entity query set object with predefined filters
    :arg list filter_choices: list of valid choices, used to sanitize the content of `filters`
    :arg list filters: the filters to get and combine
    :arg *args: arguments that will be passed to the filter methods of the EntityQuerySet class

    :returns: a combination of django ORM Q() objects containing all the required filters

    """
    # We first sanitize the list sent by the user and restrict it to only values we accept.
    sanitized_filters = filter(lambda s: s in filter_choices, filters)

    filters = [Q()]
    for filter_name in sanitized_filters:
        filters.append(getattr(entities, filter_name.replace("-", "_"))(*args))

    # Combine all generated filters with an OR operator.
    # `operator.ior` is the pipe (|) Python operator, which turns into a logical OR
    # when used between django ORM query objects.
    return reduce(operator.ior, filters)


def get_word_count(string):
    """Compute the number of words in a given string."""
    return len(re.findall(r"[\w,.-]+", string))


@property
def user_profile_url(self):
    return reverse(
        "pontoon.contributors.contributor.username", kwargs={"username": self.username}
    )


def user_gravatar_url(self, size):
    email = hashlib.md5(self.email.lower().encode("utf-8")).hexdigest()
    data = {
        "s": str(size),
        "d": "https://ui-avatars.com/api/{name}/{size}/{background}/{color}".format(
            name=quote(self.display_name),
            size=size,
            background="333941",
            color="FFFFFF",
        ),
    }

    return "//www.gravatar.com/avatar/{email}?{data}".format(
        email=email, data=urlencode(data)
    )


@property
def user_gravatar_url_small(self):
    return user_gravatar_url(self, 88)


@property
def user_name_or_email(self):
    return self.first_name or self.email


@property
def user_contact_email(self):
    return self.profile.contact_email or self.email


@property
def user_display_name(self):
    return self.first_name or self.email.split("@")[0]


@property
def user_display_name_and_email(self):
    name = self.display_name
    return f"{name} <{self.email}>"


@classmethod
def user_display_name_or_blank(cls, user):
    """Shorcut function that displays user info if user isn't none."""
    return user.name_or_email if user else ""


@property
def user_translator_for_locales(self):
    """A list of locales, in which the user is assigned Translator permissions.

    Only includes explicitly assigned locales for superusers.
    """
    locales = []

    for group in self.groups.all():
        locale = group.translated_locales.first()
        if locale:
            locales.append(locale)

    return locales


@property
def user_manager_for_locales(self):
    """A list of locales, in which the user is assigned Manager permissions.

    Only includes explicitly assigned locales for superusers.
    """
    locales = []

    for group in self.groups.all():
        locale = group.managed_locales.first()
        if locale:
            locales.append(locale)

    return locales


@property
def user_translated_locales(self):
    """A list of locale codes the user has permission to translate.

    Includes all locales for superusers.
    """
    return get_objects_for_user(
        self, "base.can_translate_locale", accept_global_perms=False
    )


@property
def user_managed_locales(self):
    """A list of locale codes the user has permission to manage.

    Includes all locales for superusers.
    """
    return get_objects_for_user(
        self, "base.can_manage_locale", accept_global_perms=False
    )


@property
def user_translated_projects(self):
    """
    Returns a map of permission for every user
    :param self:
    :return:
    """
    user_project_locales = (
        get_objects_for_user(
            self, "base.can_translate_project_locale", accept_global_perms=False
        )
    ).values_list("pk", flat=True)

    project_locales = ProjectLocale.objects.filter(
        has_custom_translators=True
    ).values_list("pk", "locale__code", "project__slug")
    permission_map = {
        f"{locale}-{project}": (pk in user_project_locales)
        for pk, locale, project in project_locales
    }
    return permission_map


def user_role(self, managers=None, translators=None):
    """
    Prefetched managers and translators dicts help reduce the number of queries
    on pages that contain a lot of users, like the Top Contributors page.
    """
    if self.is_superuser:
        return "Admin"

    if self.pk is None or self.profile.system_user:
        return "System User"

    if managers is not None:
        if self in managers:
            return "Manager for " + ", ".join(managers[self])
    else:
        if self.managed_locales:
            return "Manager for " + ", ".join(
                self.managed_locales.values_list("code", flat=True)
            )

    if translators is not None:
        if self in translators:
            return "Translator for " + ", ".join(translators[self])
    else:
        if self.translated_locales:
            return "Translator for " + ", ".join(
                self.translated_locales.values_list("code", flat=True)
            )

    return "Contributor"


def user_locale_role(self, locale):
    if self.is_superuser:
        return "Admin"
    if self.pk is None or self.profile.system_user:
        return "System User"
    if self in locale.managers_group.user_set.all():
        return "Manager"
    if self in locale.translators_group.user_set.all():
        return "Translator"
    else:
        return "Contributor"


@property
def contributed_translations(self):
    """Filtered contributions provided by user."""
    return Translation.objects.filter(user=self)


@property
def top_contributed_locale(self):
    """Locale the user has made the most contributions to."""
    try:
        return (
            self.translation_set.values("locale__code")
            .annotate(total=Count("locale__code"))
            .distinct()
            .order_by("-total")
            .first()["locale__code"]
        )
    except TypeError:
        # This error is raised if `top_contribution` is null. That happens if the user
        # has never contributed to any locales.
        return None


def can_translate(self, locale, project):
    """Check if user has suitable permissions to translate in given locale or project/locale."""

    # Locale managers can translate all projects
    if locale in self.managed_locales:
        return True

    project_locale = ProjectLocale.objects.get(project=project, locale=locale)
    if project_locale.has_custom_translators:
        return self.has_perm("base.can_translate_project_locale", project_locale)

    return self.has_perm("base.can_translate_locale", locale)


def is_new_contributor(self, locale):
    """Return True if the user hasn't made contributions to the locale yet."""
    return (
        not self.translation_set.filter(locale=locale)
        .exclude(entity__resource__project__system_project=True)
        .exists()
    )


@property
def menu_notifications(self):
    """A list of notifications to display in the notifications menu."""
    unread_count = self.notifications.unread().count()
    count = settings.NOTIFICATIONS_MAX_COUNT

    if unread_count > count:
        count = unread_count

    return self.notifications.prefetch_related("actor", "target", "action_object")[
        :count
    ]


@property
def unread_notifications_display(self):
    """Textual representation of the unread notifications count."""
    count = self.notifications.unread().count()

    if count > 9:
        return "9+"

    return count


@property
def serialized_notifications(self):
    """Serialized list of notifications to display in the notifications menu."""
    unread_count = self.notifications.unread().count()
    count = settings.NOTIFICATIONS_MAX_COUNT
    notifications = []

    if unread_count > count:
        count = unread_count

    for notification in self.notifications.prefetch_related(
        "actor", "target", "action_object"
    )[:count]:
        actor = None
        is_comment = False

        if hasattr(notification.actor, "slug"):
            if "new string" in notification.verb:
                actor = {
                    "anchor": notification.actor.name,
                    "url": reverse(
                        "pontoon.translate.locale.agnostic",
                        kwargs={
                            "slug": notification.actor.slug,
                            "part": "all-resources",
                        },
                    )
                    + "?status=missing,pretranslated",
                }
            else:
                actor = {
                    "anchor": notification.actor.name,
                    "url": reverse(
                        "pontoon.projects.project",
                        kwargs={"slug": notification.actor.slug},
                    ),
                }
        elif hasattr(notification.actor, "email"):
            actor = {
                "anchor": notification.actor.name_or_email,
                "url": reverse(
                    "pontoon.contributors.contributor.username",
                    kwargs={"username": notification.actor.username},
                ),
            }

        target = None
        if notification.target:
            t = notification.target
            # New string or Manual notification
            if hasattr(t, "slug"):
                target = {
                    "anchor": t.name,
                    "url": reverse(
                        "pontoon.projects.project",
                        kwargs={"slug": t.slug},
                    ),
                }

            # Comment notifications
            elif hasattr(t, "resource"):
                is_comment = True
                target = {
                    "anchor": t.resource.project.name,
                    "url": reverse(
                        "pontoon.translate",
                        kwargs={
                            "locale": notification.action_object.code,
                            "project": t.resource.project.slug,
                            "resource": t.resource.path,
                        },
                    )
                    + f"?string={t.pk}",
                }

        notifications.append(
            {
                "id": notification.id,
                "level": notification.level,
                "unread": notification.unread,
                "description": {
                    "content": notification.description,
                    "is_comment": is_comment,
                },
                "verb": notification.verb,
                "date": notification.timestamp.strftime("%b %d, %Y %H:%M"),
                "date_iso": notification.timestamp.isoformat(),
                "actor": actor,
                "target": target,
            }
        )

    return {
        "has_unread": unread_count > 0,
        "notifications": notifications,
        "unread_count": str(self.unread_notifications_display),
    }


def user_serialize(self):
    """Serialize Project contact"""

    return {
        "avatar": self.gravatar_url_small,
        "name": self.name_or_email,
        "url": self.profile_url,
    }


@property
def latest_action(self):
    """
    Return the date of the latest user activity (translation submission or review).
    """
    try:
        return ActionLog.objects.filter(
            performed_by=self,
            action_type__startswith="translation:",
        ).latest("created_at")
    except ActionLog.DoesNotExist:
        return None


User.add_to_class("profile_url", user_profile_url)
User.add_to_class("gravatar_url", user_gravatar_url)
User.add_to_class("gravatar_url_small", user_gravatar_url_small)
User.add_to_class("name_or_email", user_name_or_email)
User.add_to_class("contact_email", user_contact_email)
User.add_to_class("display_name", user_display_name)
User.add_to_class("display_name_and_email", user_display_name_and_email)
User.add_to_class("display_name_or_blank", user_display_name_or_blank)
User.add_to_class("translator_for_locales", user_translator_for_locales)
User.add_to_class("manager_for_locales", user_manager_for_locales)
User.add_to_class("translated_locales", user_translated_locales)
User.add_to_class("managed_locales", user_managed_locales)
User.add_to_class("translated_projects", user_translated_projects)
User.add_to_class("role", user_role)
User.add_to_class("locale_role", user_locale_role)
User.add_to_class("contributed_translations", contributed_translations)
User.add_to_class("top_contributed_locale", top_contributed_locale)
User.add_to_class("can_translate", can_translate)
User.add_to_class("is_new_contributor", is_new_contributor)
User.add_to_class("menu_notifications", menu_notifications)
User.add_to_class("unread_notifications_display", unread_notifications_display)
User.add_to_class("serialized_notifications", serialized_notifications)
User.add_to_class("serialize", user_serialize)
User.add_to_class("latest_action", latest_action)


class PermissionChangelog(models.Model):
    """
    Track changes of roles added or removed from a user.
    """

    # Managers can perform various action on a user.
    class ActionType(models.TextChoices):
        # User has been added to a group (e.g. translators, managers).
        ADDED = "added", "Added"
        # User has been removed from a group (e.g. translators, managers).
        REMOVED = "removed", "Removed"

    action_type = models.CharField(max_length=20, choices=ActionType.choices)
    performed_by = models.ForeignKey(
        User, models.SET_NULL, null=True, related_name="changed_permissions_log"
    )
    performed_on = models.ForeignKey(
        User, models.SET_NULL, null=True, related_name="permisions_log"
    )
    group = models.ForeignKey(Group, models.CASCADE)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "User permissions log"
        verbose_name_plural = "Users permissions logs"
        ordering = ("pk",)

    def __repr__(self):
        return "User(pk={}) {} User(pk={}) from {}".format(
            self.performed_by_id,
            self.action_type,
            self.performed_on_id,
            self.group.name,
        )


class AggregatedStats(models.Model):
    total_strings = models.PositiveIntegerField(default=0)
    approved_strings = models.PositiveIntegerField(default=0)
    pretranslated_strings = models.PositiveIntegerField(default=0)
    strings_with_errors = models.PositiveIntegerField(default=0)
    strings_with_warnings = models.PositiveIntegerField(default=0)
    unreviewed_strings = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True

    @classmethod
    def get_stats_sum(cls, qs):
        """
        Get sum of stats for all items in the queryset.
        """
        return {
            "total_strings": sum(x.total_strings for x in qs),
            "approved_strings": sum(x.approved_strings for x in qs),
            "pretranslated_strings": sum(x.pretranslated_strings for x in qs),
            "strings_with_errors": sum(x.strings_with_errors for x in qs),
            "strings_with_warnings": sum(x.strings_with_warnings for x in qs),
            "unreviewed_strings": sum(x.unreviewed_strings for x in qs),
        }

    @classmethod
    def get_top_instances(cls, qs):
        """
        Get top instances in the queryset.
        """
        return {
            "most_strings": sorted(qs, key=lambda x: x.total_strings)[-1],
            "most_translations": sorted(qs, key=lambda x: x.approved_strings)[-1],
            "most_suggestions": sorted(qs, key=lambda x: x.unreviewed_strings)[-1],
            "most_missing": sorted(qs, key=lambda x: x.missing_strings)[-1],
        }

    def adjust_stats(
        self,
        total_strings_diff,
        approved_strings_diff,
        pretranslated_strings_diff,
        strings_with_errors_diff,
        strings_with_warnings_diff,
        unreviewed_strings_diff,
    ):
        self.total_strings = F("total_strings") + total_strings_diff
        self.approved_strings = F("approved_strings") + approved_strings_diff
        self.pretranslated_strings = (
            F("pretranslated_strings") + pretranslated_strings_diff
        )
        self.strings_with_errors = F("strings_with_errors") + strings_with_errors_diff
        self.strings_with_warnings = (
            F("strings_with_warnings") + strings_with_warnings_diff
        )
        self.unreviewed_strings = F("unreviewed_strings") + unreviewed_strings_diff

        self.save(
            update_fields=[
                "total_strings",
                "approved_strings",
                "pretranslated_strings",
                "strings_with_errors",
                "strings_with_warnings",
                "unreviewed_strings",
            ]
        )

    @property
    def missing_strings(self):
        return (
            self.total_strings
            - self.approved_strings
            - self.pretranslated_strings
            - self.strings_with_errors
            - self.strings_with_warnings
        )

    @property
    def completed_strings(self):
        return (
            self.approved_strings
            + self.pretranslated_strings
            + self.strings_with_warnings
        )

    @property
    def complete(self):
        return self.total_strings == self.completed_strings

    @property
    def completed_percent(self):
        return self.percent_of_total(self.completed_strings)

    @property
    def approved_percent(self):
        return self.percent_of_total(self.approved_strings)

    @property
    def pretranslated_percent(self):
        return self.percent_of_total(self.pretranslated_strings)

    @property
    def errors_percent(self):
        return self.percent_of_total(self.strings_with_errors)

    @property
    def warnings_percent(self):
        return self.percent_of_total(self.strings_with_warnings)

    @property
    def unreviewed_percent(self):
        return self.percent_of_total(self.unreviewed_strings)

    def percent_of_total(self, n):
        return n / self.total_strings * 100 if self.total_strings else 0


def validate_cldr(value):
    for item in value.split(","):
        try:
            number = int(item.strip())
        except ValueError:
            return
        if number < 0 or number >= len(Locale.CLDR_PLURALS):
            raise ValidationError(
                "%s must be a list of integers between 0 and 5" % value
            )


class LocaleQuerySet(models.QuerySet):
    def unsynced(self):
        """
        Filter unsynchronized locales.
        """
        return self.filter(translatedresources__isnull=True).distinct()

    def visible(self):
        """
        Visible locales have at least one TranslatedResource defined from a non
        system project.
        """
        return self.available().filter(
            pk__in=ProjectLocale.objects.visible().values_list("locale", flat=True)
        )

    def available(self):
        """
        Available locales have at least one TranslatedResource defined.
        """
        return self.filter(
            pk__in=TranslatedResource.objects.values_list("locale", flat=True)
        )

    def prefetch_project_locale(self, project):
        """
        Prefetch ProjectLocale and latest translation data for given project.
        """
        return self.prefetch_related(
            Prefetch(
                "project_locale",
                queryset=(
                    ProjectLocale.objects.filter(project=project).prefetch_related(
                        "latest_translation__user"
                    )
                ),
                to_attr="fetched_project_locale",
            )
        )

    def get_stats_sum(self):
        """
        Get sum of stats for all items in the queryset.
        """
        return AggregatedStats.get_stats_sum(self)

    def get_top_instances(self):
        """
        Get top instances in the queryset.
        """
        return AggregatedStats.get_top_instances(self)


class Locale(AggregatedStats):
    code = models.CharField(max_length=20, unique=True)

    google_translate_code = models.CharField(
        max_length=20,
        blank=True,
        help_text="""
        Google Translate maintains its own list of
        <a href="https://translate.google.com/intl/en/about/languages/">
        supported locales</a>. Choose a matching locale from the list or leave blank to disable
        support for Google Cloud Translation machine translation service.
        """,
    )

    google_automl_model = models.CharField(
        max_length=30,
        blank=True,
        help_text="""
        ID of a custom model, trained using locale translation memory. If the value is set,
        Pontoon will use the Google AutoML Translation instead of the generic Translation API.
        """,
    )

    # Codes used by optional Microsoft services
    ms_translator_code = models.CharField(
        max_length=20,
        blank=True,
        help_text="""
        Microsoft Translator maintains its own list of
        <a href="https://docs.microsoft.com/en-us/azure/cognitive-services/translator/languages">
        supported locales</a>. Choose a matching locale from the list or leave blank to disable
        support for Microsoft Translator machine translation service.
        """,
    )
    ms_terminology_code = models.CharField(
        max_length=20,
        blank=True,
        help_text="""
        Microsoft Terminology uses language codes that include both the language and
        the country/region. Choose a matching locale from the list or leave blank to disable support
        for Microsoft terminology:

        af-za, am-et, ar-dz, ar-eg, ar-sa, as-in, az-latn-az, be-by, bg-bg, bn-bd, bn-in,
        bs-cyrl-ba, bs-latn-ba, ca-es, ca-es-valencia, chr-cher-us, cs-cz, cy-gb, da-dk, de-at,
        de-ch, de-de, el-gr, en-au, en-ca, en-gb, en-hk, en-ie, en-in, en-my, en-ng, en-nz, en-ph,
        en-pk, en-sg, en-tt, en-us, en-za, es-ar, es-bo, es-cl, es-co, es-cr, es-do, es-ec, es-es,
        es-gt, es-hn, es-mx, es-ni, es-pa, es-pe, es-pr, es-py, es-sv, es-us, es-uy, es-ve, et-ee,
        eu-es, fa-ir, fi-fi, fil-ph, fo-fo, fr-be, fr-ca, fr-ch, fr-dz, fr-fr, fr-ma, fr-tn,
        fuc-latn-sn, ga-ie, gd-gb, gl-es, gu-in, guc-ve, ha-latn-ng, he-il, hi-in, hr-hr, hu-hu,
        hy-am, id-id, ig-ng, is-is, it-ch, it-it, iu-latn-ca, ja-jp, ka-ge, kk-kz, km-kh, kn-in,
        ko-kr, kok-in, ku-arab-iq, ky-kg, lb-lu, lo-la, lt-lt, lv-lv, mi-nz, mk-mk, ml-in, mn-mn,
        mr-in, ms-bn, ms-my, mt-mt, my-mm, nb-no, ne-np, nl-be, nl-nl, nn-no, nso-za, or-in,
        pa-arab-pk, pa-in, pl-pl, prs-af, ps-af, pt-br, pt-pt, quc-latn-gt, quz-pe, ro-md, ro-ro,
        ru-kz, ru-ru, rw-rw, sd-arab-pk, si-lk, sk-sk, sl-si, sp-xl, sq-al, sr-cyrl-ba, sr-cyrl-rs,
        sr-latn-me, sr-latn-rs, sv-se, sw-ke, ta-in, te-in, tg-cyrl-tj, th-th, ti-et, tk-tm, tl-ph,
        tn-za, tr-tr, tt-ru, ug-cn, uk-ua, ur-pk, uz-cyrl-uz, uz-latn-uz, vi-vn, wo-sn, xh-za,
        yo-ng, zh-cn, zh-hk, zh-sg, zh-tw, zu-za
        """,
    )

    # Fields used by optional SYSTRAN services
    systran_translate_code = models.CharField(
        max_length=20,
        blank=True,
        help_text="""
        SYSTRAN maintains its own list of
        <a href="https://platform.systran.net/index">supported locales</a>.
        Choose a matching locale from the list or leave blank to disable
        support for SYSTRAN machine translation service.
        """,
    )
    systran_translate_profile = models.CharField(
        max_length=128,
        blank=True,
        help_text="""
        SYSTRAN Profile UUID to specify the engine trained on the en-locale language pair.
        The field is updated automatically after the systran_translate_code field changes.
        """,
    )

    db_collation = models.CharField(
        max_length=20,
        blank=True,
        help_text="""
        Some of locales require to use different database collation than default ('en_US').

        <strong>Use with caution, because it may brake the search for this locale.</strong>
        """,
    )

    name = models.CharField(max_length=128)
    plural_rule = models.CharField(
        max_length=512,
        blank=True,
        help_text="""
        Plural rule is part of the plurals header in
        <a href="https://www.gnu.org/software/gettext/manual/gettext.html#Plural-forms">
        Gettext PO files
        </a>,
        that follows the <i>plural=</i> string, without the trailing semicolon.
        E.g. (n != 1)
        """,
    )

    # Locale contains references to user groups that translate or manage them.
    # Groups store respective permissions for users.
    translators_group = models.ForeignKey(
        Group, models.SET_NULL, related_name="translated_locales", null=True
    )
    managers_group = models.ForeignKey(
        Group, models.SET_NULL, related_name="managed_locales", null=True
    )

    # CLDR Plurals
    CLDR_PLURALS = (
        (0, "zero"),
        (1, "one"),
        (2, "two"),
        (3, "few"),
        (4, "many"),
        (5, "other"),
    )

    cldr_plurals = models.CharField(
        "CLDR Plurals",
        blank=True,
        max_length=11,
        validators=[validate_cldr],
        help_text="""
        A comma separated list of
        <a href="http://www.unicode.org/cldr/charts/latest/supplemental/language_plural_rules.html">
        CLDR plural categories</a>, where 0 represents zero, 1 one, 2 two, 3 few, 4 many, and 5 other.
        E.g. 1,5
        """,
    )

    script = models.CharField(
        max_length=128,
        default="Latin",
        help_text="""
        The script used by this locale. Find it in
        <a
        href="http://www.unicode.org/cldr/charts/latest/supplemental/languages_and_scripts.html">
        CLDR Languages and Scripts</a>.
        """,
    )

    # Writing direction
    class Direction(models.TextChoices):
        LEFT_TO_RIGHT = "ltr", "left-to-right"
        RIGHT_TO_LEFT = "rtl", "right-to-left"

    direction = models.CharField(
        max_length=3,
        default=Direction.LEFT_TO_RIGHT,
        choices=Direction.choices,
        help_text="""
        Writing direction of the script. Set to "right-to-left" if "rtl" value
        for the locale script is set to "YES" in
        <a href="https://github.com/unicode-cldr/cldr-core/blob/master/scriptMetadata.json">
        CLDR scriptMetadata.json</a>.
        """,
    )

    population = models.PositiveIntegerField(
        default=0,
        help_text="""
        Number of native speakers. Find locale code in
        <a href="https://github.com/unicode-org/cldr-json/blob/main/cldr-json/cldr-core/supplemental/territoryInfo.json">CLDR territoryInfo.json</a>
        and multiply its "_populationPercent" with the territory "_population".
        Repeat if multiple occurrences of locale code exist and sum products.
        """,
    )

    team_description = models.TextField(blank=True)

    #: Most recent translation approved or created for this locale.
    latest_translation = models.ForeignKey(
        "Translation",
        models.SET_NULL,
        blank=True,
        null=True,
        related_name="locale_latest",
    )

    accesskey_localization = models.BooleanField(
        default=True,
        verbose_name="Access key localization",
        help_text="""
        Allow localization of access keys if they are part of a string.
        Some locales don't translate access keys, mostly because they use non-Latin scripts.
    """,
    )

    objects = LocaleQuerySet.as_manager()

    class Meta:
        ordering = ["name", "code"]
        permissions = (
            ("can_translate_locale", "Can add translations"),
            ("can_manage_locale", "Can manage locale"),
        )

    def __str__(self):
        return self.name

    def serialize(self):
        return {
            "code": self.code,
            "name": self.name,
            "pk": self.pk,
            "nplurals": self.nplurals,
            "plural_rule": self.plural_rule,
            "cldr_plurals": self.cldr_plurals_list_string(),
            "direction": self.direction,
            "script": self.script,
            "google_translate_code": self.google_translate_code,
            "ms_translator_code": self.ms_translator_code,
            "systran_translate_code": self.systran_translate_code,
            "ms_terminology_code": self.ms_terminology_code,
        }

    def cldr_id_list(self):
        if self.cldr_plurals == "":
            return [1]
        else:
            return [int(p) for p in self.cldr_plurals.split(",")]

    def cldr_plurals_list(self):
        return map(Locale.cldr_id_to_plural, self.cldr_id_list())

    def cldr_plurals_list_string(self):
        return ", ".join(self.cldr_plurals_list())

    @classmethod
    def cldr_plural_to_id(self, cldr_plural):
        for i in self.CLDR_PLURALS:
            if i[1] == cldr_plural:
                return i[0]

    @classmethod
    def cldr_id_to_plural(self, cldr_id):
        for i in self.CLDR_PLURALS:
            if i[0] == cldr_id:
                return i[1]

    @property
    def nplurals(self):
        return len(self.cldr_id_list())

    def projects_permissions(self, user):
        """
        List of tuples that contain informations required by the locale permissions view.

        Projects are filtered by their visibility for the user.
        A row contains:
            id  - id of project locale
            slug - slug of the project
            name - name of the project
            translators - (id, first_name, email) translators assigned to a project locale.
            contributors - (id, first_name, email) people who contributed for a project in
                current locale.
            has_custom_translators - Flag if locale has a translators group for the project.
        """
        locale_projects = []

        def create_users_map(qs, keyfield):
            """
            Creates a map from query set
            """
            user_map = defaultdict(list)
            for row in qs:
                key = row.pop(keyfield)
                user_map[key].append(row)
            return user_map

        project_locales = list(
            self.project_locale.visible()
            .visible_for(user)
            .prefetch_related("project", "translators_group")
            .order_by("project__name")
            .values(
                "id",
                "project__pk",
                "project__name",
                "project__slug",
                "translators_group__pk",
                "has_custom_translators",
            )
        )

        projects_translators = create_users_map(
            (
                User.objects.filter(
                    groups__pk__in=[
                        project_locale["translators_group__pk"]
                        for project_locale in project_locales
                    ]
                )
                .exclude(email="")
                .prefetch_related("groups")
                .values("id", "first_name", "email", "groups__pk")
                .distinct()
                .order_by("email")
            ),
            "groups__pk",
        )

        for project_locale in project_locales:
            group_pk = project_locale["translators_group__pk"]
            locale_projects.append(
                (
                    project_locale["id"],
                    project_locale["project__slug"],
                    project_locale["project__name"],
                    projects_translators[group_pk],
                    project_locale["has_custom_translators"],
                )
            )

        return locale_projects

    def available_projects_list(self, user):
        """Get a list of available project slugs."""
        return list(
            self.project_set.available()
            .visible_for(user)
            .values_list("slug", flat=True)
        ) + ["all-projects"]

    def get_plural_index(self, cldr_plural):
        """Returns plural index for given cldr name."""
        cldr_id = Locale.cldr_plural_to_id(cldr_plural)
        return self.cldr_id_list().index(cldr_id)

    def get_relative_cldr_plural(self, plural_id):
        """
        Every locale supports a subset (a list) of The CLDR Plurals forms.
        In code, we store their relative position.
        """
        return Locale.cldr_id_to_plural(self.cldr_id_list()[plural_id])

    def get_latest_activity(self, project=None):
        return ProjectLocale.get_latest_activity(self, project)

    def get_chart(self, project=None):
        return ProjectLocale.get_chart(self, project)

    def aggregate_stats(self):
        TranslatedResource.objects.filter(
            resource__project__disabled=False,
            resource__project__system_project=False,
            resource__project__visibility=Project.Visibility.PUBLIC,
            resource__entities__obsolete=False,
            locale=self,
        ).distinct().aggregate_stats(self)

    def stats(self):
        """Get locale stats used in All Resources part."""
        return [
            {
                "title": "all-resources",
                "resource__path": [],
                "resource__total_strings": self.total_strings,
                "pretranslated_strings": self.pretranslated_strings,
                "strings_with_errors": self.strings_with_errors,
                "strings_with_warnings": self.strings_with_warnings,
                "unreviewed_strings": self.unreviewed_strings,
                "approved_strings": self.approved_strings,
            }
        ]

    def parts_stats(self, project):
        """Get locale-project paths with stats."""

        def get_details(parts):
            return parts.order_by("title").values(
                "title",
                "resource__path",
                "resource__deadline",
                "resource__total_strings",
                "pretranslated_strings",
                "strings_with_errors",
                "strings_with_warnings",
                "unreviewed_strings",
                "approved_strings",
            )

        translatedresources = TranslatedResource.objects.filter(
            resource__project=project, resource__entities__obsolete=False, locale=self
        ).distinct()
        details = list(
            get_details(translatedresources.annotate(title=F("resource__path")))
        )

        all_resources = ProjectLocale.objects.get(project=project, locale=self)
        details.append(
            {
                "title": "all-resources",
                "resource__path": [],
                "resource__deadline": [],
                "resource__total_strings": all_resources.total_strings,
                "pretranslated_strings": all_resources.pretranslated_strings,
                "strings_with_errors": all_resources.strings_with_errors,
                "strings_with_warnings": all_resources.strings_with_warnings,
                "unreviewed_strings": all_resources.unreviewed_strings,
                "approved_strings": all_resources.approved_strings,
            }
        )

        return details

    def save(self, *args, **kwargs):
        old = Locale.objects.get(pk=self.pk) if self.pk else None
        super().save(*args, **kwargs)

        # If SYSTRAN Translate code changes, update SYSTRAN Profile UUID.
        if old is None or old.systran_translate_code == self.systran_translate_code:
            return

        if not self.systran_translate_code:
            return

        api_key = settings.SYSTRAN_TRANSLATE_API_KEY
        server = settings.SYSTRAN_TRANSLATE_SERVER
        profile_owner = settings.SYSTRAN_TRANSLATE_PROFILE_OWNER
        if not (api_key or server or profile_owner):
            return

        url = f"{server}/translation/supportedLanguages"

        payload = {
            "key": api_key,
            "source": "en",
            "target": self.systran_translate_code,
        }

        try:
            r = requests.post(url, params=payload)
            root = json.loads(r.content)

            if "error" in root:
                log.error(
                    "Unable to retrieve SYSTRAN Profile UUID: {error}".format(
                        error=root
                    )
                )
                return

            for languagePair in root["languagePairs"]:
                for profile in languagePair["profiles"]:
                    if profile["selectors"]["owner"] == profile_owner:
                        self.systran_translate_profile = profile["id"]
                        self.save(update_fields=["systran_translate_profile"])
                        return

        except requests.exceptions.RequestException as e:
            log.error(f"Unable to retrieve SYSTRAN Profile UUID: {e}")


class ProjectQuerySet(models.QuerySet):
    def visible_for(self, user):
        """
        The visiblity of projects is determined by the role of the user:
        * Administrators can access all public and private projects
        * Other users can see only public projects
        """
        if user.is_superuser:
            return self

        return self.filter(visibility=Project.Visibility.PUBLIC)

    def available(self):
        """
        Available projects are not disabled and have at least one
        resource defined.
        """
        return self.filter(disabled=False, resources__isnull=False).distinct()

    def visible(self):
        """
        Visible projects are not disabled and have at least one
        resource defined and are not system projects.
        """
        return self.available().filter(system_project=False)

    def force_syncable(self):
        """
        Projects that can be force-synced are not disabled and use repository as their data source type.
        """
        return self.filter(disabled=False, data_source=Project.DataSource.REPOSITORY)

    def syncable(self):
        """
        Syncable projects are same as force-syncable, but must not have sync disabled.
        """
        return self.force_syncable().filter(sync_disabled=False)

    def prefetch_project_locale(self, locale):
        """
        Prefetch ProjectLocale and latest translation data for given locale.
        """
        return self.prefetch_related(
            Prefetch(
                "project_locale",
                queryset=(
                    ProjectLocale.objects.filter(locale=locale).prefetch_related(
                        "latest_translation__user"
                    )
                ),
                to_attr="fetched_project_locale",
            )
        )

    def get_stats_sum(self):
        """
        Get sum of stats for all items in the queryset.
        """
        return AggregatedStats.get_stats_sum(self)

    def get_top_instances(self):
        """
        Get top instances in the queryset.
        """
        return AggregatedStats.get_top_instances(self)


class Priority(models.IntegerChoices):
    LOWEST = 1, "Lowest"
    LOW = 2, "Low"
    NORMAL = 3, "Normal"
    HIGH = 4, "High"
    HIGHEST = 5, "Highest"


class Project(AggregatedStats):
    name = models.CharField(max_length=128, unique=True)
    slug = models.SlugField(unique=True)
    locales = models.ManyToManyField(Locale, through="ProjectLocale")

    class DataSource(models.TextChoices):
        REPOSITORY = "repository", "Repository"
        DATABASE = "database", "Database"

    data_source = models.CharField(
        max_length=255,
        default=DataSource.REPOSITORY,
        choices=DataSource.choices,
    )
    can_be_requested = models.BooleanField(
        default=True,
        help_text="""
        Allow localizers to request the project for their team.
    """,
    )

    configuration_file = models.CharField(
        null=True,
        blank=True,
        max_length=2000,
        help_text="""
        A path to the optional project configuration file, relative to the
        source string repository.
        """,
    )

    disabled = models.BooleanField(
        default=False,
        help_text="""
        Hide project from the UI and only keep it accessible from the admin.
        Disable the project instead of deleting it to keep translation memory
        and attributions. Also prevents project from syncing with VCS.
    """,
    )

    date_created = models.DateTimeField(default=timezone.now)
    date_disabled = models.DateTimeField(null=True, blank=True)

    sync_disabled = models.BooleanField(
        default=False,
        help_text="""
        Prevent project from syncing with VCS.
    """,
    )

    system_project = models.BooleanField(
        default=False,
        help_text="""
        System projects are built into Pontoon. They are accessible from the
        translate view, but hidden from dashboards.
    """,
    )

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        PUBLIC = "public", "Public"

    visibility = models.CharField(
        max_length=20,
        default=Visibility.PRIVATE,
        choices=Visibility.choices,
    )

    langpack_url = models.URLField(
        "Language pack URL",
        blank=True,
        null=True,
        help_text="""
        URL pattern for downloading language packs. Leave empty if language packs
        not available for the project. Supports {locale_code} wildcard.
    """,
    )

    # Project info
    info = models.TextField("Project info", blank=True)
    deadline = models.DateField(blank=True, null=True)
    priority = models.IntegerField(choices=Priority.choices, default=Priority.LOWEST)
    contact = models.ForeignKey(
        User,
        models.SET_NULL,
        null=True,
        blank=True,
        related_name="contact_for",
        help_text="""
        L10n driver in charge of the project
    """,
    )
    admin_notes = models.TextField(
        blank=True,
        help_text="""
        Notes only visible in Administration
    """,
    )

    # Most recent translation approved or created for this project.
    latest_translation = models.ForeignKey(
        "Translation",
        models.SET_NULL,
        blank=True,
        null=True,
        related_name="project_latest",
    )

    tags_enabled = models.BooleanField(default=True)

    pretranslation_enabled = models.BooleanField(
        default=False,
        help_text="""
        Pretranslate project strings using automated sources
        like translation memory and machine translation.
        """,
    )

    objects = ProjectQuerySet.as_manager()

    class Meta:
        permissions = (("can_manage_project", "Can manage project"),)
        ordering = ("pk",)

    def __str__(self):
        return self.name

    def serialize(self):
        return {
            "pk": self.pk,
            "name": self.name,
            "slug": self.slug,
            "info": self.info,
            "langpack_url": self.langpack_url or "",
            "contact": self.contact.serialize() if self.contact else None,
        }

    def save(self, *args, **kwargs):
        """
        When project disabled status changes, update denormalized stats
        for all project locales.
        """
        disabled_changed = False
        visibility_changed = False

        if self.pk is not None:
            try:
                original = Project.objects.get(pk=self.pk)
                if self.visibility != original.visibility:
                    visibility_changed = True
                if self.disabled != original.disabled:
                    disabled_changed = True
                    if self.disabled:
                        self.date_disabled = timezone.now()
                    else:
                        self.date_disabled = None
            except Project.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        if disabled_changed or visibility_changed:
            for locale in self.locales.all():
                locale.aggregate_stats()

    def changed_resources(self, now):
        """
        Returns a map of resource paths and their locales
        that where changed from the last sync.
        """
        resources = defaultdict(set)
        changes = ChangedEntityLocale.objects.filter(
            entity__resource__project=self, when__lte=now
        ).prefetch_related("locale", "entity__resource")

        for change in changes:
            resources[change.entity.resource.path].add(change.locale)

        return resources

    @cached_property
    def unsynced_locales(self):
        """
        Project Locales that haven't been synchronized yet.
        """
        return list(
            set(self.locales.all())
            - set(Locale.objects.filter(translatedresources__resource__project=self))
        )

    @property
    def needs_sync(self):
        """
        True if the project has changed since the last sync such that
        another sync is required.
        """
        changes = ChangedEntityLocale.objects.filter(entity__resource__project=self)
        return changes.exists() or self.unsynced_locales

    @property
    def checkout_path(self):
        """Path where this project's VCS checkouts are located."""
        return os.path.join(settings.MEDIA_ROOT, "projects", self.slug)

    # For compatibility with the old sync, these properties refer to the
    # first repository by ID.
    def _repo_compat_attr(self, attribute):
        repo = self.repositories.first()
        return getattr(repo, attribute) if repo is not None else None

    @property
    def repository_type(self):
        return self._repo_compat_attr("type")

    @property
    def repository_url(self):
        return self._repo_compat_attr("url")

    @property
    def repository_path(self):
        return self._repo_compat_attr("checkout_path")

    def repository_for_path(self, path):
        """
        Return the repository instance whose checkout contains the given
        path. If no matching repo is found, raise a ValueError.
        """
        repo = utils.first(
            self.repositories.all(), lambda r: path.startswith(r.checkout_path)
        )

        if repo is None:
            raise ValueError(f"Could not find repo matching path {path}.")
        else:
            return repo

    @property
    def has_multi_locale_repositories(self):
        for repo in self.repositories.all():
            if repo.multi_locale:
                return True

        return False

    @property
    def has_single_repo(self):
        return self.repositories.count() == 1

    @cached_property
    def source_repository(self):
        """
        Returns an instance of repository which contains the path to source files.
        """
        if not self.has_single_repo:
            from pontoon.sync.vcs.models import VCSProject

            source_directories = VCSProject.SOURCE_DIR_SCORES.keys()

            for repo in self.repositories.all():
                last_directory = os.path.basename(
                    os.path.normpath(urlparse(repo.url).path)
                )
                if repo.source_repo or last_directory in source_directories:
                    return repo

        return self.repositories.first()

    def translation_repositories(self):
        """
        Returns a list of project repositories containing translations.
        """
        pks = [
            repo.pk
            for repo in self.repositories.all()
            if repo.is_translation_repository
        ]
        return Repository.objects.filter(pk__in=pks)

    def get_latest_activity(self, locale=None):
        return ProjectLocale.get_latest_activity(self, locale)

    def get_chart(self, locale=None):
        return ProjectLocale.get_chart(self, locale)

    def aggregate_stats(self):
        TranslatedResource.objects.filter(
            resource__project=self, resource__entities__obsolete=False
        ).distinct().aggregate_stats(self)

    @property
    def avg_string_count(self):
        return int(self.total_strings / self.enabled_locales)

    def resource_priority_map(self):
        """
        Returns a map of resource paths and highest priorities of resource tags.
        """
        resource_priority = {}

        resource_priority_qs = self.tag_set.prefetch_related("resources").values(
            "resources__path", "priority"
        )

        for item in resource_priority_qs:
            path = item["resources__path"]
            if (
                path in resource_priority
                and resource_priority[path] >= item["priority"]
            ):
                continue
            resource_priority[path] = item["priority"]

        return resource_priority

    def available_locales_list(self):
        """Get a list of available locale codes."""
        return list(self.locales.all().values_list("code", flat=True))


class UserProfile(models.Model):
    # This field is required.
    user = models.OneToOneField(
        User, models.CASCADE, related_name="profile", primary_key=True
    )

    # Personal information
    username = models.SlugField(unique=True, blank=True, null=True)
    contact_email = models.EmailField("Contact email address", blank=True, null=True)
    contact_email_verified = models.BooleanField(default=False)
    bio = models.TextField(max_length=160, blank=True, null=True)

    # External accounts
    chat = models.CharField("Chat username", max_length=255, blank=True, null=True)
    github = models.CharField("GitHub username", max_length=255, blank=True, null=True)
    bugzilla = models.EmailField("Bugzilla email address", blank=True, null=True)

    # Visibility
    class Visibility(models.TextChoices):
        ALL = "Public", "Public"
        TRANSLATORS = "Translators", "Users with translator rights"

    class VisibilityLoggedIn(models.TextChoices):
        LOGGED_IN = "Logged-in users", "Logged-in users"
        TRANSLATORS = "Translators", "Users with translator rights"

    visibility_email = models.CharField(
        "Email address",
        max_length=20,
        default=VisibilityLoggedIn.TRANSLATORS,
        choices=VisibilityLoggedIn.choices,
    )

    visibility_external_accounts = models.CharField(
        "External accounts",
        max_length=20,
        default=Visibility.TRANSLATORS,
        choices=Visibility.choices,
    )

    visibility_self_approval = models.CharField(
        "Self-approval rate",
        max_length=20,
        default=Visibility.ALL,
        choices=Visibility.choices,
    )

    visibility_approval = models.CharField(
        "Approval rate",
        max_length=20,
        default=Visibility.ALL,
        choices=Visibility.choices,
    )

    # Notification subscriptions
    new_string_notifications = models.BooleanField(default=True)
    project_deadline_notifications = models.BooleanField(default=True)
    comment_notifications = models.BooleanField(default=True)
    unreviewed_suggestion_notifications = models.BooleanField(default=True)
    review_notifications = models.BooleanField(default=True)
    new_contributor_notifications = models.BooleanField(default=True)

    # Translation settings
    quality_checks = models.BooleanField(default=True)
    force_suggestions = models.BooleanField(default=False)

    # Used to redirect a user to a custom team page.
    custom_homepage = models.CharField(max_length=20, blank=True, null=True)

    # Used to display strings from preferred source locale.
    preferred_source_locale = models.CharField(max_length=20, blank=True, null=True)

    # Defines the order of locales displayed in locale tab.
    locales_order = ArrayField(
        models.PositiveIntegerField(),
        default=list,
        blank=True,
    )

    # Used to dismiss promotional banner for the Pontoon Add-On.
    has_dismissed_addon_promotion = models.BooleanField(default=False)

    # Used to keep track of start/step no. of user tour.
    # Not started:0, Completed: -1, Finished Step No. otherwise
    tour_status = models.IntegerField(default=0)

    # Used to mark users as system users.
    system_user = models.BooleanField(default=False)

    @property
    def preferred_locales(self):
        return Locale.objects.filter(pk__in=self.locales_order)

    @property
    def sorted_locales(self):
        locales = self.preferred_locales
        return sorted(locales, key=lambda locale: self.locales_order.index(locale.pk))


class ExternalResource(models.Model):
    """
    Represents links to external project resources like staging websites,
    production websites, development builds, production builds, screenshots,
    langpacks, etc. or team resources like style guides, dictionaries,
    glossaries, etc.
    Has no relation to the Resource class.
    """

    locale = models.ForeignKey(Locale, models.CASCADE, blank=True, null=True)
    project = models.ForeignKey(Project, models.CASCADE, blank=True, null=True)
    name = models.CharField(max_length=32)
    url = models.URLField("URL", blank=True)

    def __str__(self):
        return self.name


class ProjectLocaleQuerySet(models.QuerySet):
    def aggregated_stats(self):
        return self.aggregate(
            total_strings=Sum("total_strings"),
            approved_strings=Sum("approved_strings"),
            pretranslated_strings=Sum("pretranslated_strings"),
            strings_with_errors=Sum("strings_with_errors"),
            strings_with_warnings=Sum("strings_with_warnings"),
            unreviewed_strings=Sum("unreviewed_strings"),
        )

    def visible_for(self, user):
        """
        Filter project locales by the visibility of their projects.
        """
        if user.is_superuser:
            return self

        return self.filter(
            project__visibility=Project.Visibility.PUBLIC,
        )

    def visible(self):
        """
        Visible project locales belong to visible projects.
        """
        return self.filter(
            project__disabled=False,
            project__resources__isnull=False,
            project__system_project=False,
        ).distinct()


class ProjectLocale(AggregatedStats):
    """Link between a project and a locale that is active for it."""

    project = models.ForeignKey(Project, models.CASCADE, related_name="project_locale")
    locale = models.ForeignKey(Locale, models.CASCADE, related_name="project_locale")
    readonly = models.BooleanField(default=False)
    pretranslation_enabled = models.BooleanField(default=False)

    #: Most recent translation approved or created for this project in
    #: this locale.
    latest_translation = models.ForeignKey(
        "Translation",
        models.SET_NULL,
        blank=True,
        null=True,
        related_name="project_locale_latest",
    )

    # ProjectLocale contains references to user groups that translate them.
    # Groups store respective permissions for users.
    translators_group = models.ForeignKey(
        Group, models.SET_NULL, related_name="projectlocales", null=True
    )

    # Defines if locale has a translators group for the specific project.
    has_custom_translators = models.BooleanField(
        default=False,
    )

    objects = ProjectLocaleQuerySet.as_manager()

    class Meta:
        unique_together = ("project", "locale")
        ordering = ("pk",)
        permissions = (("can_translate_project_locale", "Can add translations"),)

    def __str__(self):
        return "{project} / {locale}".format(
            project=self.project.name,
            locale=self.locale.code,
        )

    @classmethod
    def get_latest_activity(cls, self, extra=None):
        """
        Get the latest activity within project, locale
        or combination of both.

        :param self: object to get data for,
            instance of Project or Locale
        :param extra: extra filter to be used,
            instance of Project or Locale
        """
        latest_translation = None

        if getattr(self, "fetched_project_locale", None):
            if self.fetched_project_locale:
                latest_translation = self.fetched_project_locale[0].latest_translation

        elif extra is None:
            latest_translation = self.latest_translation

        else:
            project = self if isinstance(self, Project) else extra
            locale = self if isinstance(self, Locale) else extra
            project_locale = utils.get_object_or_none(
                ProjectLocale, project=project, locale=locale
            )

            if project_locale is not None:
                latest_translation = project_locale.latest_translation

        return latest_translation.latest_activity if latest_translation else None

    @classmethod
    def get_chart(cls, self, extra=None):
        """
        Get chart for project, locale or combination of both.

        :param self: object to get data for,
            instance of Project or Locale
        :param extra: extra filter to be used,
            instance of Project or Locale
        """
        chart = None

        if getattr(self, "fetched_project_locale", None):
            if self.fetched_project_locale:
                chart = cls.get_chart_dict(self.fetched_project_locale[0])

        elif extra is None:
            chart = cls.get_chart_dict(self)

        else:
            project = self if isinstance(self, Project) else extra
            locale = self if isinstance(self, Locale) else extra
            project_locale = utils.get_object_or_none(
                ProjectLocale, project=project, locale=locale
            )

            if project_locale is not None:
                chart = cls.get_chart_dict(project_locale)

        return chart

    @classmethod
    def get_chart_dict(cls, obj):
        """Get chart data dictionary"""
        if obj.total_strings:
            return {
                "total_strings": obj.total_strings,
                "approved_strings": obj.approved_strings,
                "pretranslated_strings": obj.pretranslated_strings,
                "strings_with_errors": obj.strings_with_errors,
                "strings_with_warnings": obj.strings_with_warnings,
                "unreviewed_strings": obj.unreviewed_strings,
                "approved_share": round(obj.approved_percent),
                "pretranslated_share": round(obj.pretranslated_percent),
                "errors_share": round(obj.errors_percent),
                "warnings_share": round(obj.warnings_percent),
                "unreviewed_share": round(obj.unreviewed_percent),
                "completion_percent": int(math.floor(obj.completed_percent)),
            }

    def aggregate_stats(self):
        TranslatedResource.objects.filter(
            resource__project=self.project,
            resource__project__disabled=False,
            resource__entities__obsolete=False,
            locale=self.locale,
        ).distinct().aggregate_stats(self)


def repository_url_validator(url):
    # Regular URLs
    validator = URLValidator(["http", "https", "ftp", "ftps", "ssh", "svn+ssh"])

    # Git SCP-like URL
    pattern = r"git@[\w\.@]+[/:][\w-]+/[\w-]+(.git)?/?"

    try:
        validator(url)
    except ValidationError:
        if not re.match(pattern, url):
            raise ValidationError("Invalid URL")


class Repository(models.Model):
    """
    A remote VCS repository that stores resource files for a project.
    """

    class Type(models.TextChoices):
        GIT = "git", "Git"
        HG = "hg", "HG"
        SVN = "svn", "SVN"

    project = models.ForeignKey(Project, models.CASCADE, related_name="repositories")
    type = models.CharField(max_length=255, default=Type.GIT, choices=Type.choices)
    url = models.CharField(
        "URL", max_length=2000, validators=[repository_url_validator]
    )
    branch = models.CharField("Branch", blank=True, max_length=2000)

    website = models.URLField("Public Repository Website", blank=True, max_length=2000)

    # TODO: We should be able to remove this once we have persistent storage
    permalink_prefix = models.CharField(
        "Download prefix or path to TOML file",
        blank=True,
        max_length=2000,
        help_text="""
        A URL prefix for downloading localized files. For GitHub repositories,
        select any localized file on GitHub, click Raw and replace locale code
        and the following bits in the URL with `{locale_code}`. If you use a
        project configuration file, you need to provide the path to the raw TOML
        file on GitHub.
    """,
    )

    """
    Mapping of locale codes to VCS revisions of each repo at the last
    sync. If this isn't a multi-locale repo, the mapping has a single
    key named "single_locale" with the revision.
    """
    last_synced_revisions = JSONField(blank=True, default=dict)

    source_repo = models.BooleanField(
        default=False,
        help_text="""
        If true, this repo contains the source strings directly in the
        root of the repo. Checkouts of this repo will have "templates"
        appended to the end of their path so that they are detected as
        source directories.
    """,
    )

    def __repr__(self):
        repo_kind = "Repository"
        if self.source_repo:
            repo_kind = "SourceRepository"
        return f"<{repo_kind}[{self.pk}:{self.type}:{self.url}]"

    @property
    def multi_locale(self):
        """
        Checks if url contains locale code variable. System will replace
        this variable by the locale codes of all enabled locales for the
        project during pulls and commits.
        """
        return "{locale_code}" in self.url

    @property
    def is_source_repository(self):
        """
        Returns true if repository contains source strings.
        """
        return self == self.project.source_repository

    @property
    def is_translation_repository(self):
        """
        Returns true if repository contains translations.
        """
        return self.project.has_single_repo or not self.is_source_repository

    @property
    def checkout_path(self):
        """
        Path where the checkout for this repo is located. Does not
        include a trailing path separator.
        """
        path_components = [self.project.checkout_path]

        # Include path components from the URL in case it has locale
        # information, like https://hg.mozilla.org/gaia-l10n/fr/.
        # No worry about overlap between repos, any overlap of locale
        # directories is an error already.
        path_components += urlparse(self.url).path.split("/")
        if self.multi_locale:
            path_components = [c for c in path_components if c != "{locale_code}"]

        if self.source_repo:
            path_components.append("templates")

        # Remove trailing separator for consistency.
        return os.path.join(*path_components).rstrip(os.sep)

    @cached_property
    def api_config(self):
        """
        Repository API configuration consists of:
        - Endpoint: A URL pattern to get repository metadata from. Used during sync for faster
          retrieval of latest commit hashes in combination with the Key.
          Supports {locale_code} wildcard.
        - Key: A string used to retrieve the latest commit hash from the JSON response.
        """
        url = self.url

        if url.startswith("ssh://hg.mozilla.org/"):
            parsed_url = urlparse(url)
            endpoint = "https://{netloc}/{path}/json-rev/default".format(
                netloc=parsed_url.netloc, path=parsed_url.path.strip("/")
            )
            return {
                "endpoint": endpoint,
                "get_key": lambda x: x["node"],
            }

        if url.startswith("ssh://hg@bitbucket.org/"):
            parsed_url = urlparse(url)
            endpoint = "https://api.bitbucket.org/2.0/repositories/{path}/commit/default".format(
                path=parsed_url.path.strip("/")
            )
            return {
                "endpoint": endpoint,
                "get_key": lambda x: x["hash"],
            }

        return None

    def locale_checkout_path(self, locale):
        """
        Path where the checkout for the given locale for this repo is
        located. If this repo is not a multi-locale repo, a ValueError
        is raised.
        """
        if not self.multi_locale:
            raise ValueError(
                "Cannot get locale_checkout_path for non-multi-locale repos."
            )

        return os.path.join(self.checkout_path, locale.code)

    def locale_url(self, locale):
        """
        URL for the repo for the given locale. If this repo is not a
        multi-locale repo, a ValueError is raised.
        """
        if not self.multi_locale:
            raise ValueError("Cannot get locale_url for non-multi-locale repos.")

        return self.url.format(locale_code=locale.code)

    def url_for_path(self, path):
        """
        Determine the locale-specific repo URL for the given path.

        If this is not a multi-locale repo, raise a ValueError. If no
        repo is found for the given path, also raise a ValueError.
        """
        for locale in self.project.locales.all():
            if path.startswith(self.locale_checkout_path(locale)):
                return self.locale_url(locale)

        raise ValueError(f"No repo found for path: {path}")

    def pull(self, locales=None):
        """
        Pull changes from VCS. Returns the revision(s) of the repo after
        pulling.
        """
        from pontoon.sync.vcs.repositories import (
            get_revision,
            PullFromRepositoryException,
            update_from_vcs,
        )

        if not self.multi_locale:
            update_from_vcs(self.type, self.url, self.checkout_path, self.branch)
            return {"single_locale": get_revision(self.type, self.checkout_path)}
        else:
            current_revisions = {}
            locales = locales or self.project.locales.all()

            for locale in locales:
                repo_type = self.type
                url = self.locale_url(locale)
                checkout_path = self.locale_checkout_path(locale)
                repo_branch = self.branch

                try:
                    update_from_vcs(repo_type, url, checkout_path, repo_branch)
                    current_revisions[locale.code] = get_revision(
                        repo_type, checkout_path
                    )
                except PullFromRepositoryException as e:
                    log.error(f"{repo_type.upper()} Pull Error for {url}: {e}")

            return current_revisions

    def commit(self, message, author, path):
        """Commit changes to VCS."""
        # For multi-locale repos, figure out which sub-repo corresponds
        # to the given path.
        url = self.url
        if self.multi_locale:
            url = self.url_for_path(path)

        from pontoon.sync.vcs.repositories import commit_to_vcs

        return commit_to_vcs(self.type, path, message, author, self.branch, url)

    def set_last_synced_revisions(self, locales=None):
        """
        Set last_synced_revisions to a dictionary of revisions
        that are currently downloaded on the disk.
        """
        from pontoon.sync.vcs.repositories import get_revision

        current_revisions = {}

        if self.multi_locale:
            for locale in self.project.locales.all():
                if locales is not None and locale not in locales:
                    revision = self.last_synced_revisions.get(locale.code)
                else:
                    revision = get_revision(
                        self.type, self.locale_checkout_path(locale)
                    )

                if revision:
                    current_revisions[locale.code] = revision

        else:
            current_revisions["single_locale"] = get_revision(
                self.type, self.checkout_path
            )

        self.last_synced_revisions = current_revisions
        self.save(update_fields=["last_synced_revisions"])

    def get_last_synced_revisions(self, locale=None):
        """
        Get revision from the last_synced_revisions dictionary if exists.
        """
        if self.last_synced_revisions:
            key = locale or "single_locale"
            return self.last_synced_revisions.get(key)
        else:
            return None

    class Meta:
        unique_together = ("project", "url")
        ordering = ["id"]


class ResourceQuerySet(models.QuerySet):
    def asymmetric(self):
        return self.filter(format__in=Resource.ASYMMETRIC_FORMATS)


class Resource(models.Model):
    project = models.ForeignKey(Project, models.CASCADE, related_name="resources")
    path = models.TextField()  # Path to localization file
    total_strings = models.PositiveIntegerField(default=0)
    obsolete = models.BooleanField(default=False)

    date_created = models.DateTimeField(default=timezone.now)
    date_obsoleted = models.DateTimeField(null=True, blank=True)

    # Format
    class Format(models.TextChoices):
        DTD = "dtd", "dtd"
        FTL = "ftl", "ftl"
        INC = "inc", "inc"
        INI = "ini", "ini"
        JSON = "json", "json"
        LANG = "lang", "lang"
        PO = "po", "po"
        PROPERTIES = "properties", "properties"
        XLF = "xlf", "xliff"
        XLIFF = "xliff", "xliff"
        XML = "xml", "xml"

    format = models.CharField(
        "Format", max_length=20, blank=True, choices=Format.choices
    )

    deadline = models.DateField(blank=True, null=True)

    SOURCE_EXTENSIONS = ["pot"]  # Extensions of source-only formats.
    ALLOWED_EXTENSIONS = Format.values + SOURCE_EXTENSIONS

    ASYMMETRIC_FORMATS = {
        Format.DTD,
        Format.FTL,
        Format.INC,
        Format.INI,
        Format.JSON,
        Format.PROPERTIES,
        Format.XML,
    }

    # Formats that allow empty translations
    EMPTY_TRANSLATION_FORMATS = {
        Format.DTD,
        Format.INC,
        Format.INI,
        Format.PROPERTIES,
        Format.XML,
    }

    objects = ResourceQuerySet.as_manager()

    class Meta:
        unique_together = (("project", "path"),)

    @property
    def is_asymmetric(self):
        """Return True if this resource is in an asymmetric format."""
        return self.format in self.ASYMMETRIC_FORMATS

    @property
    def allows_empty_translations(self):
        """Return True if this resource allows empty translations."""
        return self.format in self.EMPTY_TRANSLATION_FORMATS

    def __str__(self):
        return "{project}: {resource}".format(
            project=self.project.name,
            resource=self.path,
        )

    @classmethod
    def get_path_format(self, path):
        filename, extension = os.path.splitext(path)
        path_format = extension[1:].lower()

        # Special case: pot files are considered the po format
        if path_format == "pot":
            return "po"
        elif path_format == "xlf":
            return "xliff"
        else:
            return path_format


class EntityQuerySet(models.QuerySet):
    def get_filtered_entities(
        self, locale, query, rule, project=None, match_all=True, prefetch=None
    ):
        """Return a QuerySet of values of entity PKs matching the locale, query and rule.

        Filter entities that match the given filter provided by the `locale` and `query`
        parameters. For performance reasons the `rule` parameter is also provided to filter
        entities in python instead of the DB.

        :arg Locale locale: a Locale object to get translations for
        :arg Q query: a django ORM Q() object describing translations to filter
        :arg function rule: a lambda function implementing the `query` logic
        :arg boolean match_all: if true, all plural forms must match the rule.
            Otherwise, only one matching is enough
        :arg prefetch django.db.models.Prefetch prefetch: if set, it's used to control the
            operation of prefetch_related() on the query.

        :returns: a QuerySet of values of entity PKs

        """
        # First, separately filter entities with plurals (for performance reasons)
        plural_pks = []

        if locale.nplurals:
            # For each entity with plurals, fetch translations matching the query.
            plural_candidates = self.exclude(string_plural="").prefetch_related(
                Prefetch(
                    "translation_set",
                    queryset=(
                        Translation.objects.filter(locale=locale)
                        .filter(query)
                        .prefetch_related(prefetch)
                    ),
                    to_attr="fetched_translations",
                )
            )

            # Walk through the plural forms one by one and check that:
            #  - they have a translation
            #  - the translation matches the rule
            for candidate in plural_candidates:
                count = 0
                for i in range(locale.nplurals):
                    candidate_translations = [
                        translation
                        for translation in candidate.fetched_translations
                        if translation.plural_form == i
                    ]
                    if len(candidate_translations) and rule(candidate_translations[0]):
                        count += 1

                        # No point going on if we don't care about matching all.
                        if not match_all:
                            continue

                # If `match_all` is True, we want all plural forms to have a match.
                # Otherwise, just one of them matching is enough.
                if (match_all and count == locale.nplurals) or (
                    not match_all and count
                ):
                    plural_pks.append(candidate.pk)

        translations = Translation.objects.filter(locale=locale)

        # Additional filter on the project field speeds things up because it makes faster
        # to execute a SQL subquery generated by Django.
        if project and project.slug != "all-projects":
            translations = translations.filter(entity__resource__project=project)

        # Finally, we return a query that returns both the matching entities with no
        # plurals and the entities with plurals that were stored earlier.
        return translations.filter(
            Q(Q(entity__string_plural="") & query) | Q(entity_id__in=plural_pks)
        ).values("entity")

    def missing(self, locale, project=None):
        """Return a filter to be used to select entities marked as "missing".

        An entity is marked as "missing" if at least one of its plural forms
        has no approved or pretranslated translations.

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return ~Q(
            pk__in=self.get_filtered_entities(
                locale,
                Q(approved=True) | Q(pretranslated=True),
                lambda x: x.approved or x.pretranslated,
                project=project,
            )
        )

    def warnings(self, locale, project=None):
        """Return a filter to be used to select entities with translations with warnings.

        This filter will return an entity if at least one of its plural forms
        has an approved, pretranslated or fuzzy translation with a warning.

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return Q(
            pk__in=self.get_filtered_entities(
                locale,
                (Q(approved=True) | Q(pretranslated=True) | Q(fuzzy=True))
                & Q(warnings__isnull=False),
                lambda x: (x.approved or x.pretranslated or x.fuzzy)
                and x.warnings.count(),
                match_all=False,
                prefetch=Prefetch("warnings"),
                project=project,
            )
        )

    def errors(self, locale, project=None):
        """Return a filter to be used to select entities with translations with errors.

        This filter will return an entity if at least one of its plural forms
        has an approved, pretranslated or fuzzy translation with an error.

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return Q(
            pk__in=self.get_filtered_entities(
                locale,
                (Q(approved=True) | Q(pretranslated=True) | Q(fuzzy=True))
                & Q(errors__isnull=False),
                lambda x: (x.approved or x.pretranslated or x.fuzzy)
                and x.errors.count(),
                match_all=False,
                prefetch=Prefetch("errors"),
                project=project,
            )
        )

    def pretranslated(self, locale, project=None):
        """Return a filter to be used to select entities marked as "pretranslated".

        An entity is marked as "pretranslated" if all of its plural forms have a pretranslated translation.

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return Q(
            pk__in=self.get_filtered_entities(
                locale,
                Q(pretranslated=True, warnings__isnull=True, errors__isnull=True),
                lambda x: x.pretranslated,
                project=project,
            )
        )

    def translated(self, locale, project):
        """Return a filter to be used to select entities marked as "approved".

        An entity is marked as "approved" if all of its plural forms have an approved translation.

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return Q(
            pk__in=self.get_filtered_entities(
                locale,
                Q(approved=True, warnings__isnull=True, errors__isnull=True),
                lambda x: x.approved,
                project=project,
            )
        )

    def unreviewed(self, locale, project=None):
        """Return a filter to be used to select entities with suggested translations.

        An entity is said to have suggestions if at least one of its plural forms
        has at least one unreviewed suggestion (not approved, not rejected, not pretranslated, not fuzzy).

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return Q(
            pk__in=self.get_filtered_entities(
                locale,
                Q(approved=False, rejected=False, pretranslated=False, fuzzy=False),
                lambda x: not x.approved
                and not x.rejected
                and not x.pretranslated
                and not x.fuzzy,
                match_all=False,
                project=project,
            )
        )

    def rejected(self, locale, project=None):
        """Return a filter to be used to select entities with rejected translations.

        This filter will return all entities that have a rejected translation.

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return Q(
            pk__in=self.get_filtered_entities(
                locale,
                Q(rejected=True),
                lambda x: x.rejected,
                match_all=False,
                project=project,
            )
        )

    def missing_without_unreviewed(self, locale, project=None):
        """Return a filter to be used to select entities with no or only rejected translations.

        This filter will return all entities that have no or only rejected translations.
        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return ~Q(
            pk__in=self.get_filtered_entities(
                locale,
                Q(approved=True) | Q(pretranslated=True) | Q(rejected=False),
                lambda x: x.approved or x.pretranslated or not x.rejected,
                project=project,
            )
        )

    def fuzzy(self, locale, project=None):
        """Return a filter to be used to select entities marked as "fuzzy".

        An entity is marked as "fuzzy" if all of its plural forms have a fuzzy
        translation.

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return Q(
            pk__in=self.get_filtered_entities(
                locale,
                Q(fuzzy=True, warnings__isnull=True, errors__isnull=True),
                lambda x: x.fuzzy,
                project=project,
            )
        )

    def empty(self, locale, project=None):
        """Return a filter to be used to select empty translations.

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return Q(
            pk__in=self.get_filtered_entities(
                locale,
                Q(string=""),
                lambda x: x.string == "",
                match_all=False,
                project=project,
            )
        )

    def unchanged(self, locale, project=None):
        """Return a filter to be used to select entities that have unchanged translations.

        An entity is marked as "unchanged" if all of its plural forms have translations
        equal to the source string.

        :arg Locale locale: a Locale object to get translations for

        :returns: a django ORM Q object to use as a filter

        """
        return Q(
            pk__in=self.get_filtered_entities(
                locale,
                Q(active=True)
                & (
                    Q(string=F("entity__string")) | Q(string=F("entity__string_plural"))
                ),
                lambda x: x.active
                and (x.string == x.entity.string or x.string == x.entity.string_plural),
                match_all=False,
                project=project,
            )
        )

    def authored_by(self, locale, emails):
        # Validate if user passed a real email
        sanitized_emails = filter(utils.is_email, emails)
        query = Q()

        if sanitized_emails:
            query |= Q(translation__user__email__in=sanitized_emails)

        if "imported" in emails:
            query |= Q(translation__user__isnull=True)

        if sanitized_emails or "imported" in emails:
            return query & Q(translation__locale=locale)

        return Q()

    def reviewed_by(self, locale, emails):
        # Validate if user passed a real email
        sanitized_emails = filter(utils.is_email, emails)

        if sanitized_emails:
            return Q(translation__locale=locale) & (
                Q(translation__approved_user__email__in=sanitized_emails)
                | Q(translation__rejected_user__email__in=sanitized_emails)
            )

        return Q()

    def between_time_interval(self, locale, start, end):
        return Q(translation__locale=locale, translation__date__range=(start, end))

    def between_review_time_interval(self, locale, start, end):
        return Q(
            Q(translation__locale=locale)
            & (
                Q(translation__approved_date__range=(start, end))
                | Q(translation__rejected_date__range=(start, end))
            )
        )

    def prefetch_active_translations(self, locale):
        """
        Prefetch active translations for given locale.
        """
        return self.prefetch_related(
            Prefetch(
                "translation_set",
                queryset=(
                    Translation.objects.filter(
                        locale=locale, active=True
                    ).prefetch_related(
                        "errors",
                        "warnings",
                    )
                ),
                to_attr="active_translations",
            )
        )

    def prefetch_alternative_originals(self, code):
        """
        Prefetch approved translations for given preferred source locale.
        """
        return self.prefetch_related(
            Prefetch(
                "translation_set",
                queryset=(Translation.objects.filter(locale__code=code, approved=True)),
                to_attr="alternative_originals",
            )
        )

    def reset_active_translations(self, locale):
        """
        Reset active translation for given set of entities and locale.
        """
        translations = Translation.objects.filter(
            entity__in=self,
            locale=locale,
        )

        # First, deactivate all translations
        translations.update(active=False)

        # Mark all approved, pretranslated and fuzzy translations as active.
        translations.filter(
            Q(approved=True) | Q(pretranslated=True) | Q(fuzzy=True)
        ).update(active=True)

        # Mark most recent unreviewed suggestions without active siblings
        # for any given combination of (locale, entity, plural_form) as active.
        unreviewed_pks = set()
        unreviewed = translations.filter(
            approved=False,
            pretranslated=False,
            fuzzy=False,
            rejected=False,
        ).values_list("entity", "plural_form")

        for entity, plural_form in unreviewed:
            siblings = (
                Translation.objects.filter(
                    entity=entity,
                    locale=locale,
                    plural_form=plural_form,
                )
                .exclude(rejected=True)
                .order_by("-active", "-date")
            )
            if siblings and not siblings[0].active:
                unreviewed_pks.add(siblings[0].pk)

        translations.filter(pk__in=unreviewed_pks).update(active=True)

    def get_or_create(self, defaults=None, **kwargs):
        kwargs["word_count"] = get_word_count(kwargs["string"])
        return super().get_or_create(defaults=defaults, **kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        if "string" in fields:
            for obj in objs:
                obj.word_count = get_word_count(obj.string)
            if "word_count" not in fields:
                fields.append("word_count")
        super().bulk_update(objs, fields=fields, batch_size=batch_size)


class Entity(DirtyFieldsMixin, models.Model):
    resource = models.ForeignKey(Resource, models.CASCADE, related_name="entities")
    string = models.TextField()
    string_plural = models.TextField(blank=True)
    # Unique identifier, used to compare DB and VCS objects
    key = models.TextField()
    # Format-specific value, used to provide more context
    context = models.TextField(blank=True)
    comment = models.TextField(blank=True)
    group_comment = models.TextField(blank=True)
    resource_comment = models.TextField(blank=True)
    order = models.PositiveIntegerField(default=0)
    source = JSONField(blank=True, default=list)  # List of paths to source code files
    obsolete = models.BooleanField(default=False)
    word_count = models.PositiveIntegerField(default=0)

    date_created = models.DateTimeField(default=timezone.now)
    date_obsoleted = models.DateTimeField(null=True, blank=True)

    changed_locales = models.ManyToManyField(
        Locale,
        through="ChangedEntityLocale",
        help_text="List of locales in which translations for this entity have "
        "changed since the last sync.",
    )
    objects = EntityQuerySet.as_manager()

    class Meta:
        index_together = (("resource", "obsolete", "string_plural"),)

    @property
    def cleaned_key(self):
        """
        Get cleaned key, without the source string and Translate Toolkit
        separator.
        """
        key = self.key.split(KEY_SEPARATOR)[0]
        if key == self.string:
            key = ""

        return key

    def __str__(self):
        return self.string

    def save(self, *args, **kwargs):
        self.word_count = get_word_count(self.string)
        super().save(*args, **kwargs)

    def get_stats(self, locale):
        """
        Get stats for a single (entity, locale) pair.

        :arg Locale locale: filter translations for this locale.
        :return: a dictionary with stats for an Entity, all keys are suffixed with `_diff` to
            make them easier to pass into adjust_all_stats.
        """
        translations = list(
            self.translation_set.filter(locale=locale).prefetch_related(
                "errors",
                "warnings",
            )
        )

        approved_strings_count = len(
            [
                t
                for t in translations
                if t.approved and not (t.errors.exists() or t.warnings.exists())
            ]
        )

        pretranslated_strings_count = len(
            [
                t
                for t in translations
                if t.pretranslated and not (t.errors.exists() or t.warnings.exists())
            ]
        )

        if self.string_plural:
            approved = int(approved_strings_count == locale.nplurals)
            pretranslated = int(pretranslated_strings_count == locale.nplurals)

        else:
            approved = int(approved_strings_count > 0)
            pretranslated = int(pretranslated_strings_count > 0)

        if not (approved or pretranslated):
            has_errors = bool(
                [
                    t
                    for t in translations
                    if (t.approved or t.pretranslated or t.fuzzy) and t.errors.exists()
                ]
            )
            has_warnings = bool(
                [
                    t
                    for t in translations
                    if (t.approved or t.pretranslated or t.fuzzy)
                    and t.warnings.exists()
                ]
            )

            errors = int(has_errors)
            warnings = int(has_warnings)

        else:
            errors = 0
            warnings = 0

        unreviewed_count = len(
            [
                t
                for t in translations
                if not (t.approved or t.pretranslated or t.fuzzy or t.rejected)
            ]
        )

        return {
            "total_strings_diff": 0,
            "approved_strings_diff": approved,
            "pretranslated_strings_diff": pretranslated,
            "strings_with_errors_diff": errors,
            "strings_with_warnings_diff": warnings,
            "unreviewed_strings_diff": unreviewed_count,
        }

    @classmethod
    def get_stats_diff(cls, stats_before, stats_after):
        """
        Return stat difference between the two states of the entity.

        :arg dict stats_before: dict returned by get_stats() for the initial state.
        :arg dict stats_after: dict returned by get_stats() for the current state.
        :return: dictionary with differences between provided stats.
        """
        return {
            stat_name: stats_after[stat_name] - stats_before[stat_name]
            for stat_name in stats_before
        }

    def has_changed(self, locale):
        """
        Check if translations in the given locale have changed since the
        last sync.
        """
        return locale in self.changed_locales.all()

    def get_active_translation(self, plural_form=None):
        """
        Get active translation for a given entity and plural form.
        Active translations must be prefetched for the requested locale.
        """
        translations = self.active_translations

        if plural_form is not None:
            translations = [t for t in translations if t.plural_form == plural_form]

        return translations[0] if translations else Translation()

    def reset_active_translation(self, locale, plural_form=None):
        """
        Reset active translation for given entity, locale and plural form.
        Return active translation if exists or empty Translation instance.
        """
        translations = self.translation_set.filter(locale=locale)

        if plural_form is not None:
            translations = translations.filter(plural_form=plural_form)

        translations.update(active=False)

        candidates = translations.filter(rejected=False).order_by(
            "-approved", "-pretranslated", "-fuzzy", "-date"
        )

        if candidates:
            active_translation = candidates[0]
            active_translation.active = True

            # Do not trigger the overridden Translation.save() method
            super(Translation, active_translation).save(update_fields=["active"])

            return active_translation
        else:
            return Translation()

    def reset_term_translation(self, locale):
        """
        When translation in the "Terminology" project changes, update the corresponding
        TermTranslation:
        - If approved translation exists, use it as TermTranslation
        - If approved translation doesn't exist, remove any TermTranslation instance

        This method is also executed in the process of deleting a term translation,
        because it needs to be rejected first, which triggers the call to this
        function.
        """
        term = self.term

        try:
            approved_translation = self.translation_set.get(
                locale=locale, approved=True
            )
            term_translation, _ = term.translations.get_or_create(locale=locale)
            term_translation.text = approved_translation.string
            term_translation.save(update_fields=["text"])
        except Translation.DoesNotExist:
            term.translations.filter(locale=locale).delete()

    @classmethod
    def for_project_locale(
        self,
        user,
        project,
        locale,
        paths=None,
        status=None,
        tag=None,
        search=None,
        exclude_entities=None,
        extra=None,
        time=None,
        author=None,
        review_time=None,
        reviewer=None,
        exclude_self_reviewed=None,
    ):
        """Get project entities with locale translations."""

        # Time & author filters have to be applied before the aggregation
        # (with_status_counts) and the status & extra filters to avoid
        # unnecessary joins causing performance and logic issues.
        pre_filters = []
        post_filters = []

        if time:
            if re.match("^[0-9]{12}-[0-9]{12}$", time):
                start, end = utils.parse_time_interval(time)
                pre_filters.append(
                    Entity.objects.between_time_interval(locale, start, end)
                )

        if review_time:
            if re.match("^[0-9]{12}-[0-9]{12}$", review_time):
                start, end = utils.parse_time_interval(review_time)
                pre_filters.append(
                    Entity.objects.between_review_time_interval(locale, start, end)
                )

        if author:
            pre_filters.append(Entity.objects.authored_by(locale, author.split(",")))

        if reviewer:
            pre_filters.append(Entity.objects.reviewed_by(locale, reviewer.split(",")))

        if exclude_self_reviewed:
            pre_filters.append(
                ~Q(
                    Q(translation__approved_user=F("translation__user"))
                    | Q(translation__rejected_user=F("translation__user"))
                )
            )

        if pre_filters:
            entities = Entity.objects.filter(
                pk__in=Entity.objects.filter(Q(*pre_filters))
            )
        else:
            entities = Entity.objects.all()

        entities = entities.filter(
            resource__translatedresources__locale=locale,
            resource__project__disabled=False,
            obsolete=False,
        )

        if project.slug == "all-projects":
            visible_projects = Project.objects.visible_for(user)
            entities = entities.filter(
                resource__project__system_project=False,
                resource__project__in=visible_projects,
            )
        else:
            entities = entities.filter(resource__project=project)

        # Filter by path
        if paths:
            entities = entities.filter(resource__path__in=paths)

        if status:
            # Apply a combination of filters based on the list of statuses the user sent.
            status_filter_choices = (
                "missing",
                "warnings",
                "errors",
                "pretranslated",
                "translated",
                "unreviewed",
            )
            post_filters.append(
                combine_entity_filters(
                    entities, status_filter_choices, status.split(","), locale, project
                )
            )

        if extra:
            # Apply a combination of filters based on the list of extras the user sent.
            extra_filter_choices = (
                "rejected",
                "unchanged",
                "empty",
                "fuzzy",
                "missing-without-unreviewed",
            )
            post_filters.append(
                combine_entity_filters(
                    entities, extra_filter_choices, extra.split(","), locale
                )
            )

        if tag:
            post_filters.append(Q(resource__tag__slug__in=tag.split(",")))

        if post_filters:
            entities = entities.filter(Q(*post_filters))
            if tag:
                # only tag needs `distinct` as it traverses m2m fields
                entities = entities.distinct()

        # Filter by search parameters
        if search:
            search_list = utils.get_search_phrases(search)

            translation_filters = (
                Q(translation__string__icontains_collate=(search, locale.db_collation))
                & Q(translation__locale=locale)
                for search in search_list
            )
            translation_matches = entities.filter(*translation_filters).values_list(
                "id", flat=True
            )

            entity_filters = (
                Q(string__icontains=search)
                | Q(string_plural__icontains=search)
                | Q(comment__icontains=search)
                | Q(group_comment__icontains=search)
                | Q(resource_comment__icontains=search)
                | Q(key__icontains=search)
                for search in search_list
            )
            entity_matches = entities.filter(*entity_filters).values_list(
                "id", flat=True
            )

            entities = Entity.objects.filter(
                pk__in=set(list(translation_matches) + list(entity_matches))
            )

        if exclude_entities:
            entities = entities.exclude(pk__in=exclude_entities)

        order_fields = ("resource__path", "order")
        if project.slug == "all-projects":
            order_fields = ("resource__project__name",) + order_fields

        return entities.order_by(*order_fields)

    @classmethod
    def map_entities(
        cls,
        locale,
        preferred_source_locale,
        entities,
        is_sibling=False,
    ):
        entities_array = []

        # Prefetch related Translations, Resources, Projects and ProjectLocales
        entities = entities.prefetch_active_translations(locale).prefetch_related(
            Prefetch(
                "resource__project__project_locale",
                queryset=ProjectLocale.objects.filter(locale=locale),
                to_attr="projectlocale",
            )
        )

        if preferred_source_locale != "":
            entities = entities.prefetch_alternative_originals(preferred_source_locale)

        for entity in entities:
            translation_array = []

            original = entity.string
            original_plural = entity.string_plural

            if original_plural == "":
                translation = entity.get_active_translation().serialize()
                translation_array.append(translation)
            else:
                for plural_form in range(0, locale.nplurals or 1):
                    translation = entity.get_active_translation(plural_form).serialize()
                    translation_array.append(translation)

            if preferred_source_locale != "" and entity.alternative_originals:
                original = entity.alternative_originals[0].string
                if original_plural != "":
                    original_plural = entity.alternative_originals[-1].string

            entities_array.append(
                {
                    "pk": entity.pk,
                    "original": original,
                    "original_plural": original_plural,
                    "machinery_original": entity.string,
                    "key": entity.cleaned_key,
                    "context": entity.context,
                    "path": entity.resource.path,
                    "project": entity.resource.project.serialize(),
                    "format": entity.resource.format,
                    "comment": entity.comment,
                    "group_comment": entity.group_comment,
                    "resource_comment": entity.resource_comment,
                    "order": entity.order,
                    "source": entity.source,
                    "obsolete": entity.obsolete,
                    "translation": translation_array,
                    "readonly": entity.resource.project.projectlocale[0].readonly,
                    "is_sibling": is_sibling,
                }
            )

        return entities_array


class ChangedEntityLocale(models.Model):
    """
    ManyToMany model for storing what locales have changed translations for a
    specific entity since the last sync.
    """

    entity = models.ForeignKey(Entity, models.CASCADE)
    locale = models.ForeignKey(Locale, models.CASCADE)
    when = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("entity", "locale")


def extra_default():
    """Default value for the Translation.extra field."""
    return {}


class TranslationQuerySet(models.QuerySet):
    def translated_resources(self, locale):
        return TranslatedResource.objects.filter(
            resource__entities__translation__in=self, locale=locale
        ).distinct()

    def authors(self):
        """
        Return a list of translation authors.
        """
        # *Important*
        # pontoon.contributors.utils depends on a few models from pontoon.base.models and causes a
        # circular dependency.
        from pontoon.contributors.utils import users_with_translations_counts

        return [
            {
                "email": user.email,
                "display_name": user.name_or_email,
                "id": user.id,
                "gravatar_url": user.gravatar_url(88),
                "translation_count": user.translations_count,
                "role": user.user_role,
            }
            for user in users_with_translations_counts(None, Q(id__in=self))
        ]

    def counts_per_minute(self):
        """
        Return a dictionary of translation counts per minute.
        """
        translations = (
            self.extra({"minute": "date_trunc('minute', date)"})
            .order_by("minute")
            .values("minute")
            .annotate(count=Count("id"))
        )

        data = []
        for period in translations:
            data.append([utils.convert_to_unix_time(period["minute"]), period["count"]])
        return data

    def for_checks(self, only_db_formats=True):
        """
        Return an optimized queryset for `checks`-related functions.
        :arg bool only_db_formats: filter translations by formats supported by checks.
        """
        translations = self.prefetch_related(
            "entity__resource__entities",
            "locale",
        )

        if only_db_formats:
            translations = translations.filter(
                entity__resource__format__in=DB_FORMATS,
            )

        return translations

    def bulk_mark_changed(self):
        changed_entities = {}
        existing = ChangedEntityLocale.objects.values_list(
            "entity", "locale"
        ).distinct()

        for translation in self.exclude(
            entity__resource__project__data_source=Project.DataSource.DATABASE
        ):
            key = (translation.entity.pk, translation.locale.pk)

            if key not in existing:
                changed_entities[key] = ChangedEntityLocale(
                    entity=translation.entity, locale=translation.locale
                )

        ChangedEntityLocale.objects.bulk_create(changed_entities.values())


class Translation(DirtyFieldsMixin, models.Model):
    entity = models.ForeignKey(Entity, models.CASCADE)
    locale = models.ForeignKey(Locale, models.CASCADE)
    user = models.ForeignKey(User, models.SET_NULL, null=True, blank=True)
    string = models.TextField()
    # Index of Locale.cldr_plurals_list()
    plural_form = models.SmallIntegerField(null=True, blank=True)
    date = models.DateTimeField(default=timezone.now)

    # Active translations are displayed in the string list and as the first
    # entry in the History tab. There can only be one active translation for
    # each (entity, locale, plural_form) combination. See bug 1481175.
    active = models.BooleanField(default=False)

    pretranslated = models.BooleanField(default=False)
    fuzzy = models.BooleanField(default=False)

    approved = models.BooleanField(default=False)
    approved_user = models.ForeignKey(
        User,
        models.SET_NULL,
        related_name="approved_translations",
        null=True,
        blank=True,
    )
    approved_date = models.DateTimeField(null=True, blank=True)

    unapproved_user = models.ForeignKey(
        User,
        models.SET_NULL,
        related_name="unapproved_translations",
        null=True,
        blank=True,
    )
    unapproved_date = models.DateTimeField(null=True, blank=True)

    rejected = models.BooleanField(default=False)
    rejected_user = models.ForeignKey(
        User,
        models.SET_NULL,
        related_name="rejected_translations",
        null=True,
        blank=True,
    )
    rejected_date = models.DateTimeField(null=True, blank=True)

    unrejected_user = models.ForeignKey(
        User,
        models.SET_NULL,
        related_name="unrejected_translations",
        null=True,
        blank=True,
    )
    unrejected_date = models.DateTimeField(null=True, blank=True)

    class MachinerySource(models.TextChoices):
        TRANSLATION_MEMORY = "translation-memory", "Translation Memory"
        CONCORDANCE_SEARCH = "concordance-search", "Concordance Search"
        GOOGLE_TRANSLATE = "google-translate", "Google Translate"
        MICROSOFT_TRANSLATOR = "microsoft-translator", "Microsoft Translator"
        SYSTRAN_TRANSLATE = "systran-translate", "Systran Translate"
        MICROSOFT_TERMINOLOGY = "microsoft-terminology", "Microsoft"
        CAIGHDEAN = "caighdean", "Caighdean"

    machinery_sources = ArrayField(
        models.CharField(max_length=30, choices=MachinerySource.choices),
        default=list,
        blank=True,
    )

    objects = TranslationQuerySet.as_manager()

    # extra stores data that we want to save for the specific format
    # this translation is stored in, but that we otherwise don't care
    # about.
    extra = JSONField(default=extra_default)

    class Meta:
        index_together = (
            ("entity", "user", "approved", "pretranslated"),
            ("entity", "locale", "approved"),
            ("entity", "locale", "pretranslated"),
            ("entity", "locale", "fuzzy"),
            ("locale", "user", "entity"),
            ("date", "locale"),
        )
        constraints = [
            models.UniqueConstraint(
                name="entity_locale_plural_form_active",
                fields=["entity", "locale", "plural_form", "active"],
                condition=Q(active=True),
            ),
            # The rule above doesn't catch the plural_form = None case
            models.UniqueConstraint(
                name="entity_locale_active",
                fields=["entity", "locale", "active"],
                condition=Q(active=True, plural_form__isnull=True),
            ),
        ]

    @classmethod
    def for_locale_project_paths(self, locale, project, paths):
        """
        Return Translation QuerySet for given locale, project and paths.
        """
        translations = Translation.objects.filter(
            entity__obsolete=False, entity__resource__project=project, locale=locale
        )

        if paths:
            translations = translations.filter(entity__resource__path__in=paths)

        return translations

    @property
    def latest_activity(self):
        """
        Return the date and user associated with the latest activity on
        this translation.
        """
        if self.approved_date is not None and self.approved_date > self.date:
            return {
                "translation": self,
                "date": self.approved_date,
                "user": self.approved_user,
                "type": "approved",
            }
        else:
            return {
                "translation": self,
                "date": self.date,
                "user": self.user,
                "type": "submitted",
            }

    @property
    def machinery_sources_values(self):
        """
        Returns the corresponding comma-separated machinery_sources values
        """
        result = [
            self.MachinerySource(source).label for source in self.machinery_sources
        ]
        return ", ".join(result)

    @property
    def tm_source(self):
        source = self.entity.string

        if self.entity.resource.format == Resource.Format.FTL:
            return get_simple_preview(source)

        return source

    @property
    def tm_target(self):
        target = self.string

        if self.entity.resource.format == Resource.Format.FTL:
            return get_simple_preview(target)

        return target

    def __str__(self):
        return self.string

    def save(self, update_stats=True, failed_checks=None, *args, **kwargs):
        # We parametrize update of stats to make testing easier.
        if update_stats:
            stats_before = self.entity.get_stats(self.locale)

        super().save(*args, **kwargs)

        project = self.entity.resource.project

        # Only one translation can be approved at a time for any
        # Entity/Locale.
        if self.approved:
            approved_translations = Translation.objects.filter(
                entity=self.entity,
                locale=self.locale,
                plural_form=self.plural_form,
                rejected=False,
            ).exclude(pk=self.pk)

            # Log that all those translations are rejected.
            for t in approved_translations:
                log_action(
                    ActionLog.ActionType.TRANSLATION_REJECTED,
                    self.approved_user or self.user,
                    translation=t,
                )

            # Remove any TM entries of old translations that will get rejected.
            # Must be executed before translations set changes.
            TranslationMemoryEntry.objects.filter(
                translation__in=approved_translations
            ).delete()

            approved_translations.update(
                approved=False,
                approved_user=None,
                approved_date=None,
                rejected=True,
                rejected_user=self.approved_user,
                rejected_date=self.approved_date,
                pretranslated=False,
                fuzzy=False,
            )

            if not self.memory_entries.exists():
                TranslationMemoryEntry.objects.create(
                    source=self.tm_source,
                    target=self.tm_target,
                    entity=self.entity,
                    translation=self,
                    locale=self.locale,
                    project=project,
                )

        # Whenever a translation changes, mark the entity as having
        # changed in the appropriate locale. We could be smarter about
        # this but for now this is fine.
        if self.approved:
            self.mark_changed()

        if project.slug == "terminology":
            self.entity.reset_term_translation(self.locale)

        # We use get_or_create() instead of just get() to make it easier to test.
        translatedresource, _ = TranslatedResource.objects.get_or_create(
            resource=self.entity.resource, locale=self.locale
        )

        # Update latest translation where necessary
        self.update_latest_translation()

        # Failed checks must be saved before stats are updated (bug 1521606)
        if failed_checks is not None:
            save_failed_checks(self, failed_checks)

        # We parametrize update of stats to make testing easier.
        if update_stats:
            # Update stats AFTER changing approval status.
            stats_after = self.entity.get_stats(self.locale)
            stats_diff = Entity.get_stats_diff(stats_before, stats_after)
            translatedresource.adjust_all_stats(**stats_diff)

    def update_latest_translation(self):
        """
        Set `latest_translation` to this translation if its more recent than
        the currently stored translation. Do this for all affected models.
        """
        resource = self.entity.resource
        project = resource.project
        locale = self.locale

        to_update = [
            (TranslatedResource, Q(Q(resource=resource) & Q(locale=locale))),
            (ProjectLocale, Q(Q(project=project) & Q(locale=locale))),
            (Project, Q(pk=project.pk)),
        ]

        if not project.system_project:
            to_update.append((Locale, Q(pk=locale.pk)))

        for model, query in to_update:
            model.objects.filter(
                Q(
                    query
                    & Q(
                        Q(latest_translation=None)
                        | Q(latest_translation__date__lt=self.latest_activity["date"])
                    )
                )
            ).update(latest_translation=self)

    def approve(self, user):
        """
        Approve translation.
        """
        self.approved = True
        self.approved_user = user
        self.approved_date = timezone.now()

        self.pretranslated = False
        self.fuzzy = False

        self.unapproved_user = None
        self.unapproved_date = None

        self.rejected = False
        self.rejected_user = None
        self.rejected_date = None

        self.save()

        if not self.memory_entries.exists():
            TranslationMemoryEntry.objects.create(
                source=self.tm_source,
                target=self.tm_target,
                entity=self.entity,
                translation=self,
                locale=self.locale,
                project=self.entity.resource.project,
            )

        self.mark_changed()

    def unapprove(self, user):
        """
        Unapprove translation.
        """
        self.approved = False
        self.unapproved_user = user
        self.unapproved_date = timezone.now()
        self.save()

        TranslationMemoryEntry.objects.filter(translation=self).delete()
        self.mark_changed()

    def reject(self, user):
        """
        Reject translation.
        """
        # Check if translation was approved or pretranslated or fuzzy.
        # We must do this before rejecting it.
        if self.approved or self.pretranslated or self.fuzzy:
            TranslationMemoryEntry.objects.filter(translation=self).delete()
            self.mark_changed()

        self.rejected = True
        self.rejected_user = user
        self.rejected_date = timezone.now()
        self.approved = False
        self.approved_user = None
        self.approved_date = None
        self.pretranslated = False
        self.fuzzy = False
        self.save()

    def unreject(self, user):
        """
        Unreject translation.
        """
        self.rejected = False
        self.unrejected_user = user
        self.unrejected_date = timezone.now()
        self.save()

    def serialize(self):
        return {
            "pk": self.pk,
            "string": self.string,
            "approved": self.approved,
            "rejected": self.rejected,
            "pretranslated": self.pretranslated,
            "fuzzy": self.fuzzy,
            "errors": [error.message for error in self.errors.all()],
            "warnings": [warning.message for warning in self.warnings.all()],
        }

    def mark_changed(self):
        """
        Mark the given locale as having changed translations since the
        last sync.
        """
        if self.entity.resource.project.data_source == Project.DataSource.DATABASE:
            return

        ChangedEntityLocale.objects.get_or_create(
            entity=self.entity, locale=self.locale
        )


class TranslationMemoryEntryQuerySet(models.QuerySet):
    def postgres_levenshtein_ratio(
        self, text, min_quality, min_dist, max_dist, levenshtein_param=None
    ):
        """
        Filter TranslationMemory entries fully in PostgreSQL.
        `levenshtein` function is provided by `fuzzystrmatch` module.
        All strings are initially pre-filtered with min_dist and max_dist.

        :arg str text: reference string to search in Translation Memory
        :arg float min_quality: minimal quality of a levenshtein ratio
        :arg int min_dist: minimum length distance from a text string
        :arg int max_dist: maximum length distance from a text string
        :arg django.db.models.Func levenshtein_param: a field or a sql expression, the first
            argument of the levenshtein distance function. Default: the 'source' column.
        :return: TranslationMemory Entries enriched with the quality metric.
        """
        text_length = Value(len(text))

        source_target_length = Length(F("source")) + text_length

        levenshtein_param = levenshtein_param or F("source")
        levenshtein_distance_expression = LevenshteinDistance(
            levenshtein_param,
            Value(text),
            1,
            2,
            2,
        )

        entries = self.annotate(
            source_length=Length(F("source")),
            quality=ExpressionWrapper(
                (
                    Cast(
                        (source_target_length - levenshtein_distance_expression),
                        models.FloatField(),
                    )
                    / source_target_length
                )
                * 100,
                output_field=models.DecimalField(),
            ),
        ).filter(
            source_length__gte=min_dist,
            source_length__lte=max_dist,
            quality__gt=min_quality * 100,
        )
        return entries

    def python_levenshtein_ratio(self, text, min_quality, min_dist, max_dist):
        """
        Filter TranslationMemory entries in Python, with the initial pre-filtering of
        entities in the PostgreSQL.  All strings are initially pre-filtered
        with min_dist and max_dist.

        All implementations of the Levenshtein ratio algorithm have to return a QuerySet with
        annotated with the quality column.

        In the case of the in-memory (python) version, this method will make 2 queries
        to the database:
        1. initial set of pre-filtered possible matches
        2. a queryset with matches annotated with the quality column

        Extra query is made because there's no easy way to create a QuerySet
        from already fetched data and it's not possible to annotate it with additional columns.

        :arg str text: reference string to search in  TM
        :arg float min_quality: minimal quality of a levenshtein ratio
        :arg int min_dist: minimum length distance from a text string
        :arg int max_dist: maximum length distance from a text string
        :return: TranslationMemory Entries enriched with the quality metric.
        """
        # To minimalize number of entries to scan in Python. pre-filter TM entries
        # with a substring of the original string limited to 255 characters.

        possible_matches = self.postgres_levenshtein_ratio(
            text[:255],
            min_quality,
            min_dist,
            max_dist,
            Substr(F("source"), 1, 255),
        ).values_list("pk", "source")

        matches_pks = []

        # In order to keep compatibility with `postgresql_levenshtein_ratio`,
        # entries are annotate with the quality column.
        quality_sql_map = []

        for pk, source in possible_matches:
            quality = Levenshtein.ratio(text, source)

            if quality > min_quality:
                matches_pks.append(pk)
                quality_sql_map.append(When(pk=pk, then=Value(quality * 100)))

        entries = self.filter(pk__in=matches_pks,).annotate(
            quality=Case(
                *quality_sql_map,
                **dict(
                    default=Value(0),
                    output_field=models.DecimalField(),
                ),
            )
        )
        return entries

    def minimum_levenshtein_ratio(self, text, min_quality=0.7):
        """
        Returns entries that match minimal levenshtein_ratio
        """
        # Only check entities with similar length
        length = len(text)
        min_dist = int(math.ceil(max(length * min_quality, 2)))
        max_dist = int(math.floor(min(length / min_quality, 1000)))

        get_matches = self.postgres_levenshtein_ratio

        if min_dist > 255 or max_dist > 255:
            get_matches = self.python_levenshtein_ratio

        return get_matches(
            text,
            min_quality,
            min_dist,
            max_dist,
        )


class TranslationMemoryEntry(models.Model):
    source = models.TextField()
    target = models.TextField()

    entity = models.ForeignKey(
        Entity, models.SET_NULL, null=True, related_name="memory_entries"
    )
    translation = models.ForeignKey(
        Translation, models.SET_NULL, null=True, related_name="memory_entries"
    )
    locale = models.ForeignKey(Locale, models.CASCADE)
    project = models.ForeignKey(
        Project, models.SET_NULL, null=True, related_name="memory_entries"
    )

    objects = TranslationMemoryEntryQuerySet.as_manager()


class TranslatedResourceQuerySet(models.QuerySet):
    def aggregated_stats(self):
        return self.aggregate(
            total=Sum("resource__total_strings"),
            approved=Sum("approved_strings"),
            pretranslated=Sum("pretranslated_strings"),
            errors=Sum("strings_with_errors"),
            warnings=Sum("strings_with_warnings"),
            unreviewed=Sum("unreviewed_strings"),
        )

    def aggregate_stats(self, instance):
        aggregated_stats = self.aggregated_stats()

        instance.total_strings = aggregated_stats["total"] or 0
        instance.approved_strings = aggregated_stats["approved"] or 0
        instance.pretranslated_strings = aggregated_stats["pretranslated"] or 0
        instance.strings_with_errors = aggregated_stats["errors"] or 0
        instance.strings_with_warnings = aggregated_stats["warnings"] or 0
        instance.unreviewed_strings = aggregated_stats["unreviewed"] or 0

        instance.save(
            update_fields=[
                "total_strings",
                "approved_strings",
                "pretranslated_strings",
                "strings_with_errors",
                "strings_with_warnings",
                "unreviewed_strings",
            ]
        )

    def stats(self, project, paths, locale):
        """
        Returns statistics for the given project, paths and locale.
        """
        translated_resources = self.filter(
            locale=locale,
            resource__project__disabled=False,
        )

        if project.slug == "all-projects":
            translated_resources = translated_resources.filter(
                resource__project__system_project=False,
                resource__project__visibility=Project.Visibility.PUBLIC,
            )
        else:
            translated_resources = translated_resources.filter(
                resource__project=project,
            )

            if paths:
                translated_resources = translated_resources.filter(
                    resource__path__in=paths,
                )

        return translated_resources.aggregated_stats()

    def update_stats(self):
        """
        Update stats on a list of TranslatedResource.
        """
        self = self.prefetch_related("resource__project", "locale")

        locales = Locale.objects.filter(
            translatedresources__in=self,
        ).distinct()

        projects = Project.objects.filter(
            resources__translatedresources__in=self,
        ).distinct()

        projectlocales = ProjectLocale.objects.filter(
            project__resources__translatedresources__in=self,
            locale__translatedresources__in=self,
        ).distinct()

        for translated_resource in self:
            translated_resource.calculate_stats(save=False)

        TranslatedResource.objects.bulk_update(
            list(self),
            fields=[
                "total_strings",
                "approved_strings",
                "pretranslated_strings",
                "strings_with_errors",
                "strings_with_warnings",
                "unreviewed_strings",
            ],
        )

        for project in projects:
            project.aggregate_stats()

        for locale in locales:
            locale.aggregate_stats()

        for projectlocale in projectlocales:
            projectlocale.aggregate_stats()


class TranslatedResource(AggregatedStats):
    """
    Resource representation for a specific locale.
    """

    resource = models.ForeignKey(
        Resource, models.CASCADE, related_name="translatedresources"
    )
    locale = models.ForeignKey(
        Locale, models.CASCADE, related_name="translatedresources"
    )

    #: Most recent translation approved or created for this translated
    #: resource.
    latest_translation = models.ForeignKey(
        "Translation",
        models.SET_NULL,
        blank=True,
        null=True,
        related_name="resource_latest",
    )

    objects = TranslatedResourceQuerySet.as_manager()

    class Meta:
        unique_together = (("locale", "resource"),)

    def adjust_all_stats(self, *args, **kwargs):
        project = self.resource.project
        locale = self.locale

        project_locale = utils.get_object_or_none(
            ProjectLocale,
            project=project,
            locale=locale,
        )

        self.adjust_stats(*args, **kwargs)
        project.adjust_stats(*args, **kwargs)

        if not project.system_project:
            locale.adjust_stats(*args, **kwargs)

        if project_locale:
            project_locale.adjust_stats(*args, **kwargs)

    def calculate_stats(self, save=True):
        """Update stats, including denormalized ones."""
        resource = self.resource
        locale = self.locale

        entity_ids = Translation.objects.filter(locale=locale).values("entity")
        translated_entities = Entity.objects.filter(
            pk__in=entity_ids, resource=resource, obsolete=False
        )

        # Singular
        translations = Translation.objects.filter(
            entity__in=translated_entities.filter(string_plural=""),
            locale=locale,
        )

        approved = translations.filter(
            approved=True,
            errors__isnull=True,
            warnings__isnull=True,
        ).count()

        pretranslated = translations.filter(
            pretranslated=True,
            errors__isnull=True,
            warnings__isnull=True,
        ).count()

        errors = (
            translations.filter(
                Q(
                    Q(Q(approved=True) | Q(pretranslated=True) | Q(fuzzy=True))
                    & Q(errors__isnull=False)
                ),
            )
            .distinct()
            .count()
        )

        warnings = (
            translations.filter(
                Q(
                    Q(Q(approved=True) | Q(pretranslated=True) | Q(fuzzy=True))
                    & Q(warnings__isnull=False)
                ),
            )
            .distinct()
            .count()
        )

        unreviewed = translations.filter(
            approved=False,
            rejected=False,
            pretranslated=False,
            fuzzy=False,
        ).count()

        # Plural
        nplurals = locale.nplurals or 1
        for e in translated_entities.exclude(string_plural="").values_list("pk"):
            translations = Translation.objects.filter(
                entity_id=e,
                locale=locale,
            )

            plural_approved_count = translations.filter(
                approved=True,
                errors__isnull=True,
                warnings__isnull=True,
            ).count()

            plural_pretranslated_count = translations.filter(
                pretranslated=True,
                errors__isnull=True,
                warnings__isnull=True,
            ).count()

            if plural_approved_count == nplurals:
                approved += 1
            elif plural_pretranslated_count == nplurals:
                pretranslated += 1
            else:
                plural_errors_count = (
                    translations.filter(
                        Q(
                            Q(Q(approved=True) | Q(pretranslated=True) | Q(fuzzy=True))
                            & Q(errors__isnull=False)
                        ),
                    )
                    .distinct()
                    .count()
                )

                plural_warnings_count = (
                    translations.filter(
                        Q(
                            Q(Q(approved=True) | Q(pretranslated=True) | Q(fuzzy=True))
                            & Q(warnings__isnull=False)
                        ),
                    )
                    .distinct()
                    .count()
                )

                if plural_errors_count:
                    errors += 1
                elif plural_warnings_count:
                    warnings += 1

            plural_unreviewed_count = translations.filter(
                approved=False, pretranslated=False, fuzzy=False, rejected=False
            ).count()
            if plural_unreviewed_count:
                unreviewed += plural_unreviewed_count

        if not save:
            self.total_strings = resource.total_strings
            self.approved_strings = approved
            self.pretranslated_strings = pretranslated
            self.strings_with_errors = errors
            self.strings_with_warnings = warnings
            self.unreviewed_strings = unreviewed

            return False

        # Calculate diffs to reduce DB queries
        total_strings_diff = resource.total_strings - self.total_strings
        approved_strings_diff = approved - self.approved_strings
        pretranslated_strings_diff = pretranslated - self.pretranslated_strings
        strings_with_errors_diff = errors - self.strings_with_errors
        strings_with_warnings_diff = warnings - self.strings_with_warnings
        unreviewed_strings_diff = unreviewed - self.unreviewed_strings

        self.adjust_all_stats(
            total_strings_diff,
            approved_strings_diff,
            pretranslated_strings_diff,
            strings_with_errors_diff,
            strings_with_warnings_diff,
            unreviewed_strings_diff,
        )


class Comment(models.Model):
    author = models.ForeignKey(User, models.SET_NULL, null=True)
    timestamp = models.DateTimeField(default=timezone.now)
    translation = models.ForeignKey(
        Translation,
        models.CASCADE,
        related_name="comments",
        blank=True,
        null=True,
    )
    locale = models.ForeignKey(
        Locale, models.CASCADE, related_name="comments", blank=True, null=True
    )
    entity = models.ForeignKey(
        Entity, models.CASCADE, related_name="comments", blank=True, null=True
    )
    content = models.TextField()
    pinned = models.BooleanField(default=False)

    def __str__(self):
        return self.content

    def serialize(self):
        return {
            "author": self.author.name_or_email,
            "username": self.author.username,
            "user_gravatar_url_small": self.author.gravatar_url(88),
            "created_at": self.timestamp.strftime("%b %d, %Y %H:%M"),
            "date_iso": self.timestamp.isoformat(),
            "content": self.content,
            "pinned": self.pinned,
            "id": self.id,
        }

    def save(self, *args, **kwargs):
        """
        Validate Comments before saving.
        """
        if not (
            (self.translation and not self.locale and not self.entity)
            or (not self.translation and self.locale and self.entity)
        ):
            raise ValidationError("Invalid comment arguments")

        super().save(*args, **kwargs)
