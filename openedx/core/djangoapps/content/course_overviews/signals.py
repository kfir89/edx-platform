"""
Signal handler for invalidating cached course overviews
"""


import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import Signal
from django.dispatch.dispatcher import receiver

from openedx.core.djangoapps.signals.signals import COURSE_CERT_DATE_CHANGE
from xmodule.data import CertificatesDisplayBehaviors
from xmodule.modulestore.django import SignalHandler  # lint-amnesty, pylint: disable=wrong-import-order

from .models import CourseOverview

LOG = logging.getLogger(__name__)

# providing_args=["updated_course_overview", "previous_start_date"]
COURSE_START_DATE_CHANGED = Signal()
# providing_args=["updated_course_overview", "previous_self_paced"]
COURSE_PACING_CHANGED = Signal()
# providing_args=["courserun_key"]
IMPORT_COURSE_DETAILS = Signal()
# providing_args=["courserun_key"]
DELETE_COURSE_DETAILS = Signal()


@receiver(SignalHandler.course_published)
def _listen_for_course_publish(sender, course_key, **kwargs):  # pylint: disable=unused-argument
    """
    Catches the signal that a course has been published in Studio and
    updates the corresponding CourseOverview cache entry.
    """
    try:
        previous_course_overview = CourseOverview.objects.get(id=course_key)
    except CourseOverview.DoesNotExist:
        previous_course_overview = None
    updated_course_overview = CourseOverview.load_from_module_store(course_key)
    _check_for_course_changes(previous_course_overview, updated_course_overview)


@receiver(SignalHandler.course_deleted)
def _listen_for_course_delete(sender, course_key, **kwargs):  # pylint: disable=unused-argument
    """
    Catches the signal that a course has been deleted from Studio and
    invalidates the corresponding CourseOverview cache entry if one exists.
    """
    CourseOverview.objects.filter(id=course_key).delete()
    courserun_key = str(course_key)
    LOG.info(f'DELETE_COURSE_DETAILS triggered upon course_deleted signal. Key: [{courserun_key}]')
    # This signal will be handled in `federated_content_connector` plugin
    DELETE_COURSE_DETAILS.send(
        sender=None,
        courserun_key=courserun_key,
    )


@receiver(post_save, sender=CourseOverview)
def trigger_import_course_details_signal(sender, instance, created, **kwargs):  # lint-amnesty, pylint: disable=unused-argument
    """
    Triggers the `IMPORT_COURSE_DETAILS` signal which will be handled in `federated_content_connector` plugin
    """
    if created:
        courserun_key = str(instance.id)
        LOG.info(f'IMPORT_COURSE_DETAILS triggered upon CourseOverview.post_save signal. Key: [{courserun_key}]')
        IMPORT_COURSE_DETAILS.send(
            sender=None,
            courserun_key=courserun_key,
        )


def _check_for_course_changes(previous_course_overview, updated_course_overview):
    if previous_course_overview:
        _check_for_course_date_changes(previous_course_overview, updated_course_overview)
        _check_for_pacing_changes(previous_course_overview, updated_course_overview)
        _check_for_cert_availability_date_changes(previous_course_overview, updated_course_overview)


def _check_for_course_date_changes(previous_course_overview, updated_course_overview):
    if previous_course_overview.start != updated_course_overview.start:
        _log_start_date_change(previous_course_overview, updated_course_overview)
        COURSE_START_DATE_CHANGED.send(
            sender=None,
            updated_course_overview=updated_course_overview,
            previous_start_date=previous_course_overview.start,
        )


def _log_start_date_change(previous_course_overview, updated_course_overview):  # lint-amnesty, pylint: disable=missing-function-docstring
    previous_start_str = 'None'
    if previous_course_overview.start is not None:
        previous_start_str = previous_course_overview.start.isoformat()
    new_start_str = 'None'
    if updated_course_overview.start is not None:
        new_start_str = updated_course_overview.start.isoformat()
    LOG.info('Course start date changed: course={} previous={} new={}'.format(
        updated_course_overview.id,
        previous_start_str,
        new_start_str,
    ))


def _check_for_pacing_changes(previous_course_overview, updated_course_overview):
    if previous_course_overview.self_paced != updated_course_overview.self_paced:
        COURSE_PACING_CHANGED.send(
            sender=None,
            updated_course_overview=updated_course_overview,
            previous_self_paced=previous_course_overview.self_paced,
        )


def _check_for_cert_availability_date_changes(previous_course_overview, updated_course_overview):
    """
    Checks if the cert available date or the display behavior has changed in this course run. If so, emit a
    COURSE_CERT_DATE_CHANGE signal to ensure other parts of the system are aware of the change.
    """
    def _send_course_cert_date_change_signal():
        """
        A callback used to fire the COURSE_CERT_DATE_CHANGE Django signal *after* the ORM has successfully commit the
        update.
        """
        COURSE_CERT_DATE_CHANGE.send_robust(sender=None, course_key=str(updated_course_overview.id))

    course_run_id = str(updated_course_overview.id)
    prev_available_date = previous_course_overview.certificate_available_date
    prev_display_behavior = previous_course_overview.certificates_display_behavior
    prev_end_date = previous_course_overview.end  # `end_date` is a deprecated field, use `end` instead
    updated_available_date = updated_course_overview.certificate_available_date
    updated_display_behavior = updated_course_overview.certificates_display_behavior
    updated_end_date = updated_course_overview.end  # `end_date` is a deprecated field, use `end` instead

    send_signal = False
    if prev_available_date != updated_available_date:
        LOG.info(
            f"The certificate available date for {course_run_id} has changed from {prev_available_date} to "
            f"{updated_available_date}. Firing COURSE_CERT_DATE_CHANGE signal."
        )
        send_signal = True
    elif prev_display_behavior != updated_display_behavior:
        LOG.info(
            f"The certificates display behavior for {course_run_id} has changed from {prev_display_behavior} to "
            f"{updated_display_behavior}. Firing COURSE_CERT_DATE_CHANGE signal."
        )
        send_signal = True
    # edge case -- if a course run with a cert display behavior of "End date of course" has changed its end date, we
    # should fire our signal to ensure visibility of certificates managed by the Credentials IDA are corrected too
    elif (
        (prev_display_behavior == CertificatesDisplayBehaviors.END and
         updated_display_behavior == CertificatesDisplayBehaviors.END) and
        (prev_end_date != updated_end_date)
    ):
        LOG.info(
            f"The end date for {course_run_id} has changed from {prev_end_date} to {updated_end_date}. Firing "
            "COURSE_CERT_DATE_CHANGE signal."
        )
        send_signal = True

    if send_signal:
        transaction.on_commit(_send_course_cert_date_change_signal)
