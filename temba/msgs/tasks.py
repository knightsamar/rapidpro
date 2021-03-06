from __future__ import unicode_literals

import logging

from celery.signals import celeryd_init
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from django.core.cache import cache
from djcelery_transactions import task
from redis_cache import get_redis_connection
from temba.contacts.models import Contact
from temba.urls import init_analytics
from temba.utils.mage import mage_handle_new_message, mage_handle_new_contact
from .models import Msg, ExportMessagesTask, PENDING

logger = logging.getLogger(__name__)


@task(track_started=True, name='process_message_task')  # pragma: no cover
def process_message_task(msg_id, from_mage=False, new_contact=False):
    """
    Processses a single incoming message through our queue.
    """
    msg = Msg.objects.filter(pk=msg_id, status=PENDING).select_related('org', 'contact', 'contact__urns').first()

    # somebody already handled this message, move on
    if not msg:
        return

    # if message was created in Mage...
    if from_mage:
        mage_handle_new_message(msg.org, msg)
        if new_contact:
            mage_handle_new_contact(msg.org, msg.contact)

    Msg.process_message(msg)

@task(track_started=True, name='send_broadcast')
def send_broadcast_task(broadcast_id):
    try:
        # get our broadcast
        from .models import Broadcast
        broadcast = Broadcast.objects.get(pk=broadcast_id)
        broadcast.send()
    except Exception as e:
        logger.exception("Error sending broadcast: %s" % str(e))

@task(track_started=True, name='send_spam')
def send_spam(user_id, contact_id):
    """
    Processses a single incoming message through our queue.
    """
    from django.contrib.auth.models import User
    from temba.contacts.models import Contact, TEL_SCHEME
    from temba.msgs.models import Broadcast

    contact = Contact.all().get(pk=contact_id)
    user = User.objects.get(pk=user_id)
    channel = contact.org.get_send_channel(TEL_SCHEME)

    if not channel:  # pragma: no cover
        print "Sorry, no channel to be all spammy with"
        return

    long_text = "Test Message #%d. The path of the righteous man is beset on all sides by the iniquities of the " \
                "selfish and the tyranny of evil men. Blessed is your face."

    # only trigger sync on the last one
    for idx in range(10):
        broadcast = Broadcast.create(contact.org, user, long_text % (idx + 1), [contact])
        broadcast.send(trigger_send=(idx == 149))

@task(track_started=True, name='fail_old_messages')
def fail_old_messages():
    Msg.fail_old_messages()

@task(track_started=True, name='collect_message_metrics_task')
def collect_message_metrics_task():
    """
    Collects message metrics and sends them to our analytics.
    """
    from .models import INCOMING, OUTGOING, DELIVERED, SENT, WIRED, FAILED, PENDING, QUEUED, ERRORED, INITIALIZING, HANDLED
    import analytics

    r = get_redis_connection()

    # only do this if we aren't already running
    key = 'collect_message_metrics'
    if not r.get(key):
        with r.lock(key, timeout=900):
            # we use our hostname as our source so we can filter these for different brands
            context = dict(source=settings.HOSTNAME)

            # total # of delivered messages
            count = Msg.objects.filter(direction=OUTGOING, status=DELIVERED).exclude(channel=None).exclude(topup=None).count()
            analytics.track('System', 'temba.total_outgoing_delivered', properties=dict(value=count), context=context)

            # total # of sent messages (this includes delivered and wired)
            count = Msg.objects.filter(direction=OUTGOING, status__in=[DELIVERED, SENT, WIRED]).exclude(channel=None).exclude(topup=None).count()
            analytics.track('System', 'temba.total_outgoing_sent', properties=dict(value=count), context=context)

            # total # of failed messages
            count = Msg.objects.filter(direction=OUTGOING, status=FAILED).exclude(channel=None).exclude(topup=None).count()
            analytics.track('System', 'temba.total_outgoing_failed', properties=dict(value=count), context=context)

            # current # of queued messages (excluding Android)
            count = Msg.objects.filter(direction=OUTGOING, status=QUEUED).exclude(channel=None).exclude(topup=None).exclude(channel__channel_type='A').count()
            analytics.track('System', 'temba.current_outgoing_queued', properties=dict(value=count), context=context)

            # current # of initializing messages (excluding Android)
            count = Msg.objects.filter(direction=OUTGOING, status=INITIALIZING).exclude(channel=None).exclude(topup=None).exclude(channel__channel_type='A').count()
            analytics.track('System', 'temba.current_outgoing_initializing', properties=dict(value=count), context=context)

            # current # of pending messages (excluding Android)
            count = Msg.objects.filter(direction=OUTGOING, status=PENDING).exclude(channel=None).exclude(topup=None).exclude(channel__channel_type='A').count()
            analytics.track('System', 'temba.current_outgoing_pending', properties=dict(value=count), context=context)

            # current # of errored messages (excluding Android)
            count = Msg.objects.filter(direction=OUTGOING, status=ERRORED).exclude(channel=None).exclude(topup=None).exclude(channel__channel_type='A').count()
            analytics.track('System', 'temba.current_outgoing_errored', properties=dict(value=count), context=context)

            # current # of android outgoing messages waiting to be sent
            count = Msg.objects.filter(direction=OUTGOING, status__in=[PENDING, QUEUED], channel__channel_type='A').exclude(channel=None).exclude(topup=None).count()
            analytics.track('System', 'temba.current_outgoing_android', properties=dict(value=count), context=context)

            # current # of pending incoming messages that haven't yet been handled
            count = Msg.objects.filter(direction=INCOMING, status=PENDING).exclude(channel=None).count()
            analytics.track('System', 'temba.current_incoming_pending', properties=dict(value=count), context=context)

            # stuff into redis when we last run, we do this as a canary as to whether our tasks are falling behind or not running
            cache.set('last_cron', timezone.now())


@task(track_started=True, name='check_messages_task')
def check_messages_task():
    """
    Checks to see if any of our aggregators have errored messages that need to be retried.
    Also takes care of flipping Contacts from Failed to Normal and back based on their status.
    """
    from django.utils import timezone
    from .models import INCOMING, OUTGOING, PENDING, QUEUED, ERRORED, FAILED, WIRED, SENT, DELIVERED
    from temba.contacts.models import NORMAL
    from temba.orgs.models import Org
    from temba.channels.tasks import send_msg_task

    r = get_redis_connection()

    # only do this if we aren't already running
    key = 'check_messages_task'
    if not r.get(key):
        with r.lock(key, timeout=900):
            now = timezone.now()
            five_minutes_ago = now - timedelta(minutes=5)

            # get any contacts that are currently normal that had a failed message in the past five minutes
            for contact in Contact.objects.filter(msgs__created_on__gte=five_minutes_ago, msgs__direction=OUTGOING,
                                                  msgs__status=FAILED, status=NORMAL):
                # if the last message from this contact is failed, then fail this contact
                if contact.msgs.all().order_by('-created_on').first().status == FAILED:
                    contact.fail()

            # get any contacts that are currently failed that had a normal message in the past five minutes
            for contact in Contact.objects.filter(msgs__created_on__gte=five_minutes_ago, msgs__direction=OUTGOING,
                                                  msgs__status__in=[WIRED, SENT, DELIVERED], status=FAILED):
                # if the last message from this contact is ok, then mark them as normal
                if contact.msgs.all().order_by('-created_on').first().status in [WIRED, SENT, DELIVERED]:
                    contact.unfail()

            # for any org that sent messages in the past five minutes, check for pending messages
            for org in Org.objects.filter(msgs__created_on__gte=five_minutes_ago).distinct():
                org.trigger_send()

            # fire a few send msg tasks in case we dropped one somewhere during a restart
            # (these will be no-ops if there is nothing to do)
            send_msg_task.delay()
            send_msg_task.delay()

            # also check any incoming messages that are still pending somehow, reschedule them to be handled
            unhandled_messages = Msg.objects.filter(direction=INCOMING, status=PENDING, created_on__lte=five_minutes_ago)
            unhandled_messages = unhandled_messages.exclude(channel__org=None).exclude(contact__is_test=True)
            unhandled_count = unhandled_messages.count()

            if unhandled_count:
                print "** Found %d unhandled messages" % unhandled_count
                for msg in unhandled_messages:
                    msg.handle()

@celeryd_init.connect
def configure_workers(sender=None, conf=None, **kwargs):
    init_analytics()

@task(track_started=True, name='export_sms_task')
def export_sms_task(id):
    """
    Export messages to a file and e-mail a link to the user
    """
    tasks = ExportMessagesTask.objects.filter(pk=id)
    if tasks:
        task = tasks[0]
        task.do_export()
