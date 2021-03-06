# -*- coding: utf-8 -*-

from __future__ import unicode_literals

from datetime import datetime
from xlrd import xldate_as_tuple
from temba.tests import MockResponse, FlowFileTest
from django.core import mail
from django.core.urlresolvers import reverse
from djorm_hstore.models import register_hstore_handler
from smartmin.tests import SmartminTest


from django.db import connection
from mock import patch
from temba.msgs.models import INCOMING, SMS_NORMAL_PRIORITY, SMS_HIGH_PRIORITY, Label
from temba.triggers.models import Trigger
from temba.tests import TembaTest
from temba.utils import datetime_to_str
from .models import *
from temba.orgs.models import Language
import datetime

def uuid(id):
    return '00000000-00000000-00000000-%08d' % id


class RuleTest(TembaTest):

    def setUp(self):
        super(RuleTest, self).setUp()

        register_hstore_handler(connection)

        self.contact = self.create_contact('Eric', '+250788382382')
        self.contact2 = self.create_contact('Nic', '+250788383383')

        self.flow = Flow.create(self.org, self.admin, "Color Flow")

        self.other_group = self.create_group("Other", [])

        self.definition = dict(action_sets=[dict(uuid=uuid(1), x=1, y=1, destination=uuid(5),
                                            actions=[dict(type='reply', msg='What is your favorite color?')]),
                                       dict(uuid=uuid(2), x=2, y=2, destination=None,
                                            actions=[dict(type='reply', msg='I love orange too! You said: @step.value which is category: @flow.color You are: @step.contact.tel SMS: @step Flow: @flow')]),
                                       dict(uuid=uuid(3), x=3, y=3, destination=None,
                                            actions=[dict(type='reply', msg='Blue is sad. :(')]),
                                       dict(uuid=uuid(4), x=4, y=4, destination=None,
                                            actions=[dict(type='reply', msg='That is a funny color.')])
                                       ],
                          rule_sets=[dict(uuid=uuid(5), x=5, y=5,
                                          label='color',
                                          finished_key=None,
                                          operand=None,
                                          webhook=None,
                                          webhook_action=None,
                                          response_type='C',
                                          rules=[
                                              dict(uuid=uuid(12), destination=uuid(2), test=dict(type='contains', test='orange'), category="Orange"),
                                              dict(uuid=uuid(13), destination=uuid(3), test=dict(type='contains', test='blue'), category="Blue"),
                                              dict(uuid=uuid(14), destination=uuid(4), test=dict(type='true'), category="Other"),
                                              dict(uuid=uuid(15), test=dict(type='true'), category="Nothing")]) # test case with no destination
                                    ],
                          entry=uuid(1), metadata=dict(author="Ryan Lewis"))

        settings.SEND_EMAILS = True
        settings.SEND_WEBHOOKS = True

    def tearDown(self):
        super(RuleTest, self).tearDown()

        settings.SEND_EMAILS = False
        settings.SEND_WEBHOOKS = False

    def test_revision_history(self):

        # every save should result in a new flow version
        response = self.flow.update(self.definition)
        self.assertEquals(1, self.flow.versions.all().count())
        self.assertEquals(self.flow.created_by, self.flow.versions.all()[0].created_by)

        # versions should be tited to the user that created them
        self.definition['last_saved'] = response['saved_on']
        response = self.flow.update(self.definition, user=self.root)
        versions = self.flow.versions.all().order_by('-pk')
        self.assertEquals(2, versions.count())
        self.assertEquals(versions[0].created_by, self.root)

    def test_flow_lists(self):

        self.login(self.admin)

        # see our trigger on the list page
        response = self.client.get(reverse('flows.flow_list'))
        self.assertContains(response, self.flow.name)

        # archive it
        post_data = dict(action='archive', objects=self.flow.pk)
        self.client.post(reverse('flows.flow_list'), post_data)
        response = self.client.get(reverse('flows.flow_list'))
        self.assertNotContains(response, self.flow.name)

        # unarchive it
        response = self.client.get(reverse('flows.flow_archived'), post_data)
        self.assertContains(response, self.flow.name)
        post_data = dict(action='restore', objects=self.flow.pk)
        self.client.post(reverse('flows.flow_archived'), post_data)
        response = self.client.get(reverse('flows.flow_archived'), post_data)
        self.assertNotContains(response, self.flow.name)
        response = self.client.get(reverse('flows.flow_list'), post_data)
        self.assertContains(response, self.flow.name)

    def test_flow_read(self):
        self.login(self.admin)
        response = self.client.get(reverse('flows.flow_read', args=[self.flow.pk]))
        self.assertTrue('initial' in response.context)

    def test_flow_editor(self):
        self.login(self.admin)
        response = self.client.get(reverse('flows.flow_editor', args=[self.flow.pk]))
        self.assertTrue('mutable' in response.context)

    def test_states(self):
        # set our flow
        self.flow.update(self.definition)

        # how many people in the flow?
        self.assertEquals(0, self.flow.get_total_contacts())
        self.assertEquals(0, self.flow.get_completed_percentage())

        # start the flow
        self.flow.start([], [self.contact, self.contact2])

        # test our stats again
        self.assertEquals(2, self.flow.get_total_contacts())
        self.assertEquals(0, self.flow.get_completed_percentage())

        # should have created a single broadcast
        broadcast = Broadcast.objects.get()
        self.assertEquals("What is your favorite color?", broadcast.text)
        self.assertTrue(broadcast.contacts.filter(pk=self.contact.pk))
        self.assertTrue(broadcast.contacts.filter(pk=self.contact2.pk))

        # should have received a single message
        msg = Msg.objects.get(contact=self.contact)
        self.assertEquals("What is your favorite color?", msg.text)
        self.assertEquals(PENDING, msg.status)
        self.assertEquals(SMS_NORMAL_PRIORITY, msg.priority)

        # should have two steps, one for the outgoing message, another for the rule set we are now waiting on
        entry = ActionSet.objects.filter(uuid=self.flow.entry_uuid)[0]
        step = FlowStep.objects.filter(run__contact=self.contact).order_by('pk')[0]
        contact2_step = FlowStep.objects.filter(run__contact=self.contact2).order_by('pk')[1]
        self.assertEquals("Eric - A:00000000-00000000-00000000-00000001", str(step))

        # test our message context
        context = self.flow.build_message_context(self.contact, None)
        self.assertEquals(dict(__default__=''), context['flow'])

        self.login(self.admin)
        activity = json.loads(self.client.get(reverse('flows.flow_activity', args=[self.flow.pk])).content)
        self.assertEquals(2, activity['visited']["%s:%s" % (uuid(1), uuid(5))])
        self.assertEquals(2, activity['activity'][uuid(5)])

        self.assertEquals(entry.uuid, step.step_uuid)
        self.assertEquals(ACTION_SET, step.step_type)
        self.assertEquals(self.contact, step.run.contact)
        self.assertEquals(self.contact, step.contact)
        self.assertEquals(self.flow, step.run.flow)
        self.assertTrue(step.arrived_on)
        self.assertTrue(step.left_on)
        self.assertEquals(entry.destination.uuid, step.next_uuid)

        step = FlowStep.objects.filter(run__contact=self.contact).order_by('pk')[1]

        self.assertEquals(entry.destination.uuid, step.step_uuid)
        self.assertEquals(RULE_SET, step.step_type)
        self.assertEquals(self.contact, step.run.contact)
        self.assertEquals(self.contact, step.contact)
        self.assertEquals(self.flow, step.run.flow)
        self.assertTrue(step.arrived_on)
        self.assertFalse(step.left_on)
        self.assertFalse(step.messages.all())

        # if we try to get contacts at this step for our compose we should have two contacts
        self.login(self.admin)
        response = self.client.get(reverse('contacts.contact_omnibox') + "?s=%s" % step.step_uuid)
        contact_json = json.loads(response.content)
        self.assertEquals(2, len(contact_json['results']))
        self.client.logout()

        # set the flow as inactive, shouldn't react to replies
        self.flow.is_archived = True
        self.flow.save()

        # create and send a reply
        incoming = self.create_msg(direction=INCOMING, contact=self.contact, text="Orange")
        self.assertFalse(self.flow.find_and_handle(incoming))

        # no reply, our flow isn't active
        self.assertFalse(Msg.objects.filter(response_to=incoming))
        step = FlowStep.objects.get(pk=step.pk)
        self.assertFalse(step.left_on)
        self.assertFalse(step.messages.all())

        # ok, make our flow active again
        self.flow.is_archived = False
        self.flow.save()

        incoming = self.create_msg(direction=INCOMING, contact=self.contact, text="orange")
        self.assertTrue(self.flow.find_and_handle(incoming))

        # our message should have gotten a reply
        reply = Msg.objects.get(response_to=incoming)
        self.assertEquals(self.contact, reply.contact)
        self.assertEquals("I love orange too! You said: orange which is category: orange You are: 0788 382 382 SMS: orange Flow: color: orange", reply.text)

        # should be high priority
        self.assertEquals(SMS_HIGH_PRIORITY, reply.priority)

        # message context again
        context = self.flow.build_message_context(self.contact, incoming)
        self.assertTrue(context['flow'])
        self.assertEquals("orange", str(context['flow']['color']['__default__']))
        self.assertEquals("color: orange", context['flow']['__default__'])
        self.assertEquals("Orange", context['flow']['color']['category'])
        self.assertEquals("orange", context['flow']['color']['text'])

        # should have the time this value was collected
        self.assertTrue(context['flow']['color']['time'])

        self.assertEquals(self.channel.get_address_display(e164=True), context['channel']['tel_e164'])
        self.assertEquals(self.channel.get_address_display(), context['channel']['tel'])
        self.assertEquals(self.channel.get_name(), context['channel']['name'])
        self.assertEquals(self.channel.get_address_display(), context['channel']['__default__'])

        # our previous state should be executed
        step = FlowStep.objects.get(run__contact=self.contact, pk=step.id)
        self.assertTrue(step.left_on)
        self.assertEquals(step.messages.all()[0].msg_type, 'F')

        # it should contain what rule matched and what came next
        self.assertEquals(uuid(12), step.rule_uuid)
        self.assertEquals("Orange", step.rule_category)
        self.assertEquals("orange", step.rule_value)
        self.assertFalse(step.rule_decimal_value)
        self.assertEquals(uuid(2), step.next_uuid)
        self.assertTrue(incoming in step.messages.all())

        # we should also have a Value for this RuleSet
        value = Value.objects.get(run=step.run, ruleset__label="color")
        self.assertEquals(uuid(12), value.rule_uuid)
        self.assertEquals("Orange", value.category)
        self.assertEquals("orange", value.string_value)
        self.assertEquals(None, value.decimal_value)
        self.assertEquals(None, value.datetime_value)

        # check what our message context looks like now
        context = self.flow.build_message_context(self.contact, incoming)
        self.assertEquals('orange', context['flow']['color']['value'])
        self.assertEquals('Orange', context['flow']['color']['category'])
        self.assertEquals('orange', context['flow']['color']['text'])

        # change our step instead be decimal
        step.rule_value = '10'
        step.rule_decimal_value = Decimal('10')
        step.save()

        # check our message context again
        context = self.flow.build_message_context(self.contact, incoming)
        self.assertEquals('10', context['flow']['color']['value'])
        self.assertEquals('Orange', context['flow']['color']['category'])

        # this is drawn from the message which didn't change
        self.assertEquals('orange', context['flow']['color']['text'])

        # revert above change
        step.rule_value = 'orange'
        step.rule_decimal_value = None
        step.save()

        # finally we should have our final step which was our outgoing reply
        step = FlowStep.objects.filter(run__contact=self.contact).order_by('pk')[2]

        # we should have a new step
        orange_response = ActionSet.objects.get(uuid=uuid(2))

        self.assertEquals(ACTION_SET, step.step_type)
        self.assertEquals(self.contact, step.run.contact)
        self.assertEquals(self.contact, step.contact)
        self.assertEquals(self.flow, step.run.flow)
        self.assertTrue(step.arrived_on)

        # we are still in the flow, just at the last step
        self.assertFalse(step.left_on)
        self.assertFalse(step.next_uuid)

        # check our completion percentages
        self.assertEquals(2, self.flow.get_total_contacts())
        self.assertEquals(50, self.flow.get_completed_percentage())

        # at this point there are no more steps to take in the flow, so we shouldn't match anymore
        extra = self.create_msg(direction=INCOMING, contact=self.contact, text="Hello ther")
        self.assertFalse(self.flow.find_and_handle(extra))

        # check that our context processor is stuffing in our unread count
        self.login(self.admin)
        response = self.client.get(reverse('msgs.msg_inbox'))
        self.assertEquals(1, response.context['flows_unread_count'])

        # visit our list page, clears the count
        response = self.client.get(reverse('flows.flow_list'))
        self.assertEquals(0, response.context['flows_unread_count'])

        response = self.client.get(reverse('msgs.msg_inbox'))
        self.assertEquals(0, response.context['flows_unread_count'])

        self.client.logout()

        # try exporting this flow
        exported = self.client.get(reverse('flows.flow_export_results') + "?ids=%d" % self.flow.pk)
        self.assertEquals(302, exported.status_code)

        self.login(self.admin)
        exported = self.client.get(reverse('flows.flow_export_results') + "?ids=%d" % self.flow.pk)

        self.assertEquals(302, exported.status_code)

        task = ExportFlowResultsTask.objects.all()[0]

        # read it back in, check values
        from xlrd import open_workbook
        workbook = open_workbook(os.path.join(settings.MEDIA_ROOT, task.filename), 'rb')

        self.assertEquals(3, len(workbook.sheets()))
        entries = workbook.sheets()[0]
        self.assertEquals(3, entries.nrows)
        self.assertEquals(8, entries.ncols)

        messages = workbook.sheets()[2]
        self.assertEquals(6, messages.nrows)
        self.assertEquals(5, messages.ncols)

        # try getting our results
        results = self.flow.get_results()

        # should have two results
        self.assertEquals(2, len(results))

        # check the value
        found = False
        for result in results:
            if result['contact'] == self.contact:
                found = True
                self.assertEquals(1, len(result['values']))

        self.assertTrue(found)

        color = result['values'][0]
        self.assertEquals('color', color['label'])
        self.assertEquals('Orange', color['category'])
        self.assertEquals('orange', color['value'])
        self.assertEquals(uuid(5), color['node'])
        self.assertEquals(incoming.text, color['text'])

    def test_export_results_flow_with_no_response(self):
        self.login(self.admin)
        flow_missing_responses = self.create_flow()
        flow_missing_responses.update(self.definition)

        self.assertEquals(0, flow_missing_responses.get_total_contacts())
        self.assertEquals(0, flow_missing_responses.get_completed_percentage())

        # try exporting the flow without responses
        exported = self.client.get(reverse('flows.flow_export_results') + "?ids=%d" % flow_missing_responses.pk)
        self.assertEquals(302, exported.status_code)

        task = ExportFlowResultsTask.objects.all()[0]

        from xlrd import open_workbook
        workbook = open_workbook(os.path.join(settings.MEDIA_ROOT, task.filename), 'rb')

        self.assertEquals(2, len(workbook.sheets()))

        # every sheet has only the head row
        for entries in workbook.sheets():
            self.assertEquals(1, entries.nrows)
            self.assertEquals(8, entries.ncols)

    def test_copy(self):
        # save our original flow
        self.flow.update(self.definition)

        # pick a really long name so we have to concatenate
        self.flow.name = "Color Flow is a long name to use for something like this"
        self.flow.save()

        # make sure our metadata got saved
        metadata = json.loads(self.flow.metadata)
        self.assertEquals("Ryan Lewis", metadata['author'])

        # now create a copy
        copy = Flow.copy(self.flow, self.admin)

        metadata = json.loads(copy.metadata)
        self.assertEquals("Ryan Lewis", metadata['author'])

        # should have a different id
        self.assertNotEqual(self.flow.pk, copy.pk)

        # Name should start with "Copy of"
        self.assertEquals("Copy of Color Flow is a long name to use for something like thi", copy.name)

        # metadata should come out in the json
        copy_json = copy.as_json()
        self.assertEquals(dict(author="Ryan Lewis"), copy_json['metadata'])

        # should have the same number of actionsets and rulesets
        self.assertEquals(copy.action_sets.all().count(), self.flow.action_sets.all().count())
        self.assertEquals(copy.rule_sets.all().count(), self.flow.rule_sets.all().count())

    def test_optimization_reply_action(self):

        self.flow.update({"entry": "02a2f789-1545-466b-978a-4cebcc9ab89a", "rule_sets": [], "action_sets": [{"y": 0, "x": 100, "destination": None, "uuid": "02a2f789-1545-466b-978a-4cebcc9ab89a", "actions": [{"type": "api", "webhook": "https://rapidpro.io/demo/coupon/"}, {"msg": "text to get @extra.coupon", "type": "reply"}]}], "metadata": {"notes": []}})

        with patch('requests.post') as mock:
            mock.return_value = MockResponse(200, '{ "coupon": "NEXUS4" }')

            self.flow.start([], [self.contact])

            self.assertTrue(self.flow.steps())
            self.assertTrue(Msg.objects.all())
            msg = Msg.objects.all()[0]
            self.assertFalse("@extra.coupon" in msg.text)
            self.assertEquals(msg.text, "text to get NEXUS4")
            self.assertEquals(PENDING, msg.status)

    def test_parsing(self):
        # save this flow
        self.flow.update(self.definition)
        flow = Flow.objects.get(pk=self.flow.id)

        # should have created the appropriate RuleSet and ActionSet objects
        self.assertEquals(4, ActionSet.objects.all().count())

        entry = ActionSet.objects.get(uuid=uuid(1))
        actions = entry.get_actions()
        self.assertEquals(1, len(actions))
        self.assertEquals(ReplyAction('What is your favorite color?').as_json(), actions[0].as_json())
        self.assertEquals(entry.uuid, flow.entry_uuid)

        orange = ActionSet.objects.get(uuid=uuid(2))
        actions = orange.get_actions()
        self.assertEquals(1, len(actions))
        self.assertEquals(ReplyAction('I love orange too! You said: @step.value which is category: @flow.color You are: @step.contact.tel SMS: @step Flow: @flow').as_json(), actions[0].as_json())

        self.assertEquals(1, RuleSet.objects.all().count())
        ruleset = RuleSet.objects.get(uuid=uuid(5))
        self.assertEquals(entry.destination.pk, ruleset.pk)
        rules = ruleset.get_rules()
        self.assertEquals(4, len(rules))

        # check ordering
        self.assertEquals(uuid(2), rules[0].destination)
        self.assertEquals(uuid(12), rules[0].uuid)
        self.assertEquals(uuid(3), rules[1].destination)
        self.assertEquals(uuid(13), rules[1].uuid)
        self.assertEquals(uuid(4), rules[2].destination)
        self.assertEquals(uuid(14), rules[2].uuid)

        # check routing
        self.assertEquals(ContainsTest(test="orange").as_json(), rules[0].test.as_json())
        self.assertEquals(ContainsTest(test="blue").as_json(), rules[1].test.as_json())
        self.assertEquals(TrueTest().as_json(), rules[2].test.as_json())

        # and categories
        self.assertEquals("Orange", rules[0].category)
        self.assertEquals("Blue", rules[1].category)

        # back out as json
        json_dict = self.flow.as_json()

        print json.dumps(json_dict, indent=2)
        print json.dumps(self.definition, indent=2)

        self.maxDiff = None
        self.definition['last_saved'] = datetime_to_str(self.flow.saved_on)
        self.assertEquals(json_dict, self.definition)

        # remove one of our actions and rules
        del self.definition['action_sets'][3]
        del self.definition['rule_sets'][0]['rules'][2]

        # update
        self.flow.update(self.definition)

        self.assertEquals(3, ActionSet.objects.all().count())

        entry = ActionSet.objects.get(uuid=uuid(1))
        actions = entry.get_actions()
        self.assertEquals(1, len(actions))
        self.assertEquals(ReplyAction('What is your favorite color?').as_json(), actions[0].as_json())
        self.assertEquals(entry.uuid, flow.entry_uuid)

        orange = ActionSet.objects.get(uuid=uuid(2))
        actions = orange.get_actions()
        self.assertEquals(1, len(actions))
        self.assertEquals(ReplyAction('I love orange too! You said: @step.value which is category: @flow.color You are: @step.contact.tel SMS: @step Flow: @flow').as_json(), actions[0].as_json())

        self.assertEquals(1, RuleSet.objects.all().count())
        ruleset = RuleSet.objects.get(uuid=uuid(5))
        self.assertEquals(entry.destination.pk, ruleset.pk)
        rules = ruleset.get_rules()
        self.assertEquals(3, len(rules))

        # check ordering
        self.assertEquals(uuid(2), rules[0].destination)
        self.assertEquals(uuid(3), rules[1].destination)

        # check routing
        self.assertEquals(ContainsTest(test="orange").as_json(), rules[0].test.as_json())
        self.assertEquals(ContainsTest(test="blue").as_json(), rules[1].test.as_json())

        # updating with a label name that is too long should truncate it
        self.definition['rule_sets'][0]['label'] = ''.join('W' for x in range(75))
        self.definition['rule_sets'][0]['operand'] = ''.join('W' for x in range(135))
        self.definition['rule_sets'][0]['webhook'] = ''.join('W' for x in range(265))
        self.flow.update(self.definition)

        # now check they are truncated to the max lengths
        ruleset = RuleSet.objects.get(uuid=uuid(5))
        self.assertEquals(64, len(ruleset.label))
        self.assertEquals(128, len(ruleset.operand))
        self.assertEquals(255, len(ruleset.webhook_url))

    def test_expanding(self):
        # save our original flow
        self.flow.update(self.definition)

        # add actions for groups and contacts
        definition = self.flow.as_json()

        # add actions for adding to a group and messaging a contact, we'll test how these expand
        action_set = ActionSet.objects.get(uuid=uuid(4))

        actions = [AddToGroupAction([self.other_group]).as_json(),
                   SendAction("Outgoing Message", [self.other_group], [self.contact], []).as_json()]

        action_set.set_actions_dict(actions)
        action_set.save()

        # check expanding our groups
        json_dict = self.flow.as_json(expand_contacts=True)
        json_as_string = json.dumps(json_dict)

        # our json should contain the names of our contact and groups
        self.assertTrue(json_as_string.find('Eric') > 0)
        self.assertTrue(json_as_string.find('Other') > 0)

    def assertTest(self, expected_test, expected_value, test, extra=None):
        runs = FlowRun.objects.filter(contact=self.contact)
        if runs:
            run = runs[0]
        else:
            run = FlowRun.create(self.flow, self.contact)

        # clear any extra on this run
        run.fields = ""

        context = run.flow.build_message_context(run.contact, None)
        if extra:
            context['extra'] = extra

        tuple = test.evaluate(run, self.sms, context, self.sms.text)
        if expected_test:
            self.assertTrue(tuple[0])
        else:
            self.assertFalse(tuple[0])
        self.assertEquals(expected_value, tuple[1])

        # return our run for later inspection
        return run

    def assertDateTest(self, expected_test, expected_value, test):
        runs = FlowRun.objects.filter(contact=self.contact)
        if runs:
            run = runs[0]
        else:
            run = FlowRun.create(self.flow, self.contact)

        tz = run.flow.org.get_tzinfo()
        context = run.flow.build_message_context(run.contact, None)

        tuple = test.evaluate(run, self.sms, context, self.sms.text)
        if expected_test:
            self.assertTrue(tuple[0])
        else:
            self.assertFalse(tuple[0])
        if expected_test and expected_value:
            # convert our expected date time the right timezone
            expected_tz = expected_value.astimezone(tz)
            expected_value = expected_value.replace(hour=expected_tz.hour).replace(day=expected_tz.day).replace(month=expected_tz.month)
            self.assertTrue(abs((expected_value - str_to_datetime(tuple[1], tz=timezone.utc)).total_seconds()) < 60)

    def test_tests(self):
        sms = self.create_msg(contact=self.contact, text="GReen is my favorite!")
        self.sms = sms

        test = TrueTest()
        self.assertTest(True, sms.text, test)

        test = FalseTest()
        self.assertTest(False, None, test)

        test = ContainsTest(test="Green")
        self.assertTest(True, "GReen", test)

        sms.text = "Blue is my favorite"
        self.assertTest(False, None, test)

        sms.text = "Greenish is ok too"
        self.assertTest(False, None, test)

        # edit distance
        sms.text = "Greenn is ok though"
        self.assertTest(True, "Greenn", test)

        # variable substitution
        test = ContainsTest(test="@extra.color")
        sms.text = "my favorite color is GREEN today"
        self.assertTest(True, "GREEN", test, extra=dict(color="green"))

        test.test = "this THAT"
        sms.text = "this is good but won't match"
        self.assertTest(False, None, test)

        test.test = "this THAT"
        sms.text = "that and this is good and will match"
        self.assertTest(True, "this that", test)

        test = AndTest([TrueTest(), TrueTest()])
        self.assertTest(True, "that and this is good and will match", test)

        test = AndTest([TrueTest(), FalseTest()])
        self.assertTest(False, None, test)

        test = OrTest([TrueTest(), FalseTest()])
        self.assertTest(True, "that and this is good and will match", test)

        test = OrTest([FalseTest(), FalseTest()])
        self.assertTest(False, None, test)

        test = ContainsAnyTest(test="klab Kacyiru good")
        sms.text = "kLab is awesome"
        self.assertTest(True, "kLab", test)

        sms.text = "telecom is located at Kacyiru"
        self.assertTest(True, "Kacyiru", test)

        sms.text = "good morning"
        self.assertTest(True, "good", test)

        sms.text = "kLab is good"
        self.assertTest(True, "kLab good", test)

        sms.text = "kigali city"
        self.assertTest(False, None, test)

        # have the same behaviour when we have commas even a trailing one
        test = ContainsAnyTest(test="klab, kacyiru, good, ")
        sms.text = "kLab is awesome"
        self.assertTest(True, "kLab", test)

        sms.text = "telecom is located at Kacyiru"
        self.assertTest(True, "Kacyiru", test)

        sms.text = "good morning"
        self.assertTest(True, "good", test)

        sms.text = "kLab is good"
        self.assertTest(True, "kLab good", test)

        sms.text = "kigali city"
        self.assertTest(False, None, test)

        test = LtTest(test="5")
        self.assertTest(False, None, test)

        test = LteTest(test="0")
        sms.text = "My answer is -4"
        self.assertTest(True, Decimal("-4"), test)

        sms.text = "My answer is 4"
        test = LtTest(test="4")
        self.assertTest(False, None, test)

        test = GtTest(test="4")
        self.assertTest(False, None, test)

        test = GtTest(test="3")
        self.assertTest(True, Decimal("4"), test)

        test = GteTest(test="4")
        self.assertTest(True, Decimal("4"), test)

        test = GteTest(test="9")
        self.assertTest(False, None, test)

        test = EqTest(test="4")
        self.assertTest(True, Decimal("4"), test)

        test = EqTest(test="5")
        self.assertTest(False, None, test)

        test = BetweenTest(min="5", max="10")
        self.assertTest(False, None, test)

        test = BetweenTest(min="4", max="10")
        self.assertTest(True, Decimal("4"), test)

        test = BetweenTest(min="0", max="4")
        self.assertTest(True, Decimal("4"), test)

        test = BetweenTest(min="0", max="3")
        self.assertTest(False, None, test)

        sms.text = "My answer is or"
        self.assertTest(False, None, test)

        sms.text = "My answer is 4"
        test = BetweenTest(min="1", max="5")
        self.assertTest(True, Decimal("4"), test)

        sms.text = "My answer is 4rwf"
        self.assertTest(True, Decimal("4"), test)

        sms.text = "My answer is a4rwf"
        self.assertTest(False, None, test)

        test = BetweenTest(min="10", max="50")
        sms.text = "My answer is lO"
        self.assertTest(True, Decimal("10"), test)

        test = BetweenTest(min="1000", max="5000")
        sms.text = "My answer is 4,000rwf"
        self.assertTest(True, Decimal("4000"), test)

        rule = Rule(uuid(4), None, None, test)
        self.assertEquals("1000-5000", rule.get_category_name(None))

        test = StartsWithTest(test="Green")
        sms.text = "  green beans"
        self.assertTest(True, "Green", test)

        sms.text = "greenbeans"
        self.assertTest(True, "Green", test)

        sms.text = "  beans Green"
        self.assertTest(False, None, test)

        test = NumberTest()
        self.assertTest(False, None, test)

        sms.text = "I have 7"
        self.assertTest(True, Decimal("7"), test)

        # phone tests

        test = PhoneTest()
        sms.text = "My phone number is 0788 383 383"
        self.assertTest(True, "+250788383383", test)

        sms.text = "+250788123123"
        self.assertTest(True, "+250788123123", test)

        sms.text = "+12067799294"
        self.assertTest(True, "+12067799294", test)

        sms.text = "My phone is 0124515"
        self.assertTest(False, None, test)

        test = ContainsTest(test="مورنۍ")
        sms.text = "شاملیدل مورنۍ"
        self.assertTest(True, "مورنۍ", test)

        # test = "word to start" and notice "to start" is one word in arabic ataleast according to Google translate
        test = ContainsAnyTest(test="كلمة لبدء")
        # set text to "give a sample word in sentence"
        sms.text = "تعطي كلمة عينة في الجملة"
        self.assertTest(True, "كلمة", test) # we get "word"

        # we should not match "this start is not allowed" we wanted "to start"
        test = ContainsAnyTest(test="لا يسمح هذه البداية")
        self.assertTest(False, None, test)

        test = RegexTest("(?P<first_name>\w+) (\w+)")
        sms.text = "Isaac Newton"
        run = self.assertTest(True, "Isaac Newton", test)
        extra = run.field_dict()
        self.assertEquals("Isaac Newton", extra['0'])
        self.assertEquals("Isaac", extra['1'])
        self.assertEquals("Newton", extra['2'])
        self.assertEquals("Isaac", extra['first_name'])

        # find that arabic unicode is handled right
        sms.text = "مرحبا العالم"
        run = self.assertTest(True, "مرحبا العالم", test)
        extra = run.field_dict()
        self.assertEquals("مرحبا العالم", extra['0'])
        self.assertEquals("مرحبا", extra['1'])
        self.assertEquals("العالم", extra['2'])
        self.assertEquals("مرحبا", extra['first_name'])

        # no matching groups, should return whole string as match
        test = RegexTest("\w+ \w+")
        sms.text = "Isaac Newton"
        run = self.assertTest(True, "Isaac Newton", test)
        extra = run.field_dict()
        self.assertEquals("Isaac Newton", extra['0'])

        # no match, shouldn't return anything at all
        sms.text = "#$%^$#? !@#$"
        run = self.assertTest(False, None, test)
        extra = run.field_dict()
        self.assertFalse(extra)

        # no case sensitivity
        test = RegexTest("kazoo")
        sms.text = "This is my Kazoo"
        run = self.assertTest(True, "Kazoo", test)
        extra = run.field_dict()
        self.assertEquals("Kazoo", extra['0'])

        # change to have anchors
        test = RegexTest("^kazoo$")

        # no match, as at the end
        sms.text = "This is my Kazoo"
        run = self.assertTest(False, None, test)

        # this one will match
        sms.text = "Kazoo"
        run = self.assertTest(True, "Kazoo", test)
        extra = run.field_dict()
        self.assertEquals("Kazoo", extra['0'])

        def perform_date_tests(sms, dayfirst):
            """
            Performs a set of date tests in either day-first or month-first mode
            """
            self.org.date_format = 'D' if dayfirst else 'M'
            self.org.save()

            # perform all date tests as if it were 2014-01-02 03:04:05.6 UTC - a date which when localized to DD-MM-YYYY
            # or MM-DD-YYYY is ambiguous
            with patch.object(timezone, 'now', return_value=datetime.datetime(2014, 1, 2, 3, 4, 5, 6, timezone.utc)):
                now = timezone.now()
                three_days_ago = now - timedelta(days=3)
                three_days_next = now + timedelta(days=3)
                five_days_next = now + timedelta(days=5)

                sms.text = "no date in this text"
                test = HasDateTest()
                self.assertDateTest(False, None, test)

                sms.text = "123"
                self.assertDateTest(True, now.replace(year=123), test)

                sms.text = "December 14, 1892"
                self.assertDateTest(True, now.replace(year=1892, month=12, day=14), test)

                sms.text = "sometime on %d/%d/%d" % (now.day, now.month, now.year)
                self.assertDateTest(True, now, test)

                # date before/equal/after tests using old deprecated time_delta filter

                test = DateBeforeTest('@date.today|time_delta:"-1"')
                self.assertDateTest(False, None, test)

                sms.text = "this is for three days ago %d/%d/%d" % (three_days_ago.day, three_days_ago.month, three_days_ago.year)
                self.assertDateTest(True, three_days_ago, test)

                sms.text = "in the next three days %d/%d/%d" % (three_days_next.day, three_days_next.month, three_days_next.year)
                self.assertDateTest(False, None, test)

                test = DateEqualTest('@date.today|time_delta:"-3"')
                self.assertDateTest(False, None, test)

                sms.text = "this is for three days ago %d/%d/%d" % (three_days_ago.day, three_days_ago.month, three_days_ago.year)
                self.assertDateTest(True, three_days_ago, test)

                test = DateAfterTest("@date.today|time_delta:'3'")
                self.assertDateTest(False, None, test)

                sms.text = "this is for three days ago %d/%d/%d" % (five_days_next.day, five_days_next.month, five_days_next.year)
                self.assertDateTest(True, five_days_next, test)

                # date before/equal/after tests using new date arithmetic

                test = DateBeforeTest('=(date.today - 1)')
                self.assertDateTest(False, None, test)

                sms.text = "this is for three days ago %d/%d/%d" % (three_days_ago.day, three_days_ago.month, three_days_ago.year)
                self.assertDateTest(True, three_days_ago, test)

                sms.text = "in the next three days %d/%d/%d" % (three_days_next.day, three_days_next.month, three_days_next.year)
                self.assertDateTest(False, None, test)

                test = DateEqualTest('=(date.today - 3)')
                self.assertDateTest(False, None, test)

                sms.text = "this is for three days ago %d/%d/%d" % (three_days_ago.day, three_days_ago.month, three_days_ago.year)
                self.assertDateTest(True, three_days_ago, test)

                test = DateAfterTest('=(date.today + 3)')
                self.assertDateTest(False, None, test)

                sms.text = "this is for three days ago %d/%d/%d" % (five_days_next.day, five_days_next.month, five_days_next.year)
                self.assertDateTest(True, five_days_next, test)

        # check date tests in both date modes
        perform_date_tests(sms, True)
        perform_date_tests(sms, False)

    def test_length(self):
        org = self.org

        js = [dict(category="Normal Length", uuid=uuid4(), destination=uuid4(), test=dict(type='true')),
              dict(category="Way too long, will get clipped at 36 characters", uuid=uuid4(), destination=uuid4(), test=dict(type='true'))]

        rules = Rule.from_json_array(org, js)

        self.assertEquals("Normal Length", rules[0].category)
        self.assertEquals(36, len(rules[1].category))

    def test_factories(self):
        org = self.org

        js = dict(type='true')
        self.assertEquals(TrueTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, TrueTest().as_json())

        js = dict(type='false')
        self.assertEquals(FalseTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, FalseTest().as_json())

        js = dict(type='and', tests=[dict(type='true')])
        self.assertEquals(AndTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, AndTest([TrueTest()]).as_json())

        js = dict(type='or', tests=[dict(type='true')])
        self.assertEquals(OrTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, OrTest([TrueTest()]).as_json())

        js = dict(type='contains', test="green")
        self.assertEquals(ContainsTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, ContainsTest("green").as_json())

        js = dict(type='lt', test="5")
        self.assertEquals(LtTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, LtTest("5").as_json())

        js = dict(type='gt', test="5")
        self.assertEquals(GtTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, GtTest("5").as_json())

        js = dict(type='gte', test="5")
        self.assertEquals(GteTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, GteTest("5").as_json())

        js = dict(type='eq', test="5")
        self.assertEquals(EqTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, EqTest("5").as_json())

        js = dict(type='between', min="5", max="10")
        self.assertEquals(BetweenTest, Test.from_json(org, js).__class__)
        self.assertEquals(js, BetweenTest("5", "10").as_json())

        self.assertEquals(ReplyAction, Action.from_json(org, dict(type='reply', msg="hello world")).__class__)
        self.assertEquals(SendAction, Action.from_json(org, dict(type='send', msg="hello world", contacts=[], groups=[], variables=[])).__class__)

    def test_actions(self):
        msg = self.create_msg(direction=INCOMING, contact=self.contact, text="Green is my favorite")
        run = FlowRun.create(self.flow, self.contact)

        test = ReplyAction("We love green too!")
        test.execute(run, None, msg)
        msg = Msg.objects.get(contact=self.contact, direction='O')
        self.assertEquals("We love green too!", msg.text)

        Broadcast.objects.all().delete()

        action_json = test.as_json()
        test = ReplyAction.from_json(self.org, action_json)
        self.assertEquals("We love green too!", test.msg)

        test.execute(run, None, msg)

        response = msg.responses.get()
        self.assertEquals("We love green too!", response.text)
        self.assertEquals(self.contact, response.contact)

        test = SendAction("What is your favorite color?", [], [self.contact], [])
        test.execute(run, None, None)

        action_json = test.as_json()
        test = SendAction.from_json(self.org, action_json)
        self.assertEquals(test.msg, "What is your favorite color?")

        self.assertEquals(2, Broadcast.objects.all().count())

        broadcast = Broadcast.objects.all().order_by('pk')[1]
        self.assertEquals(1, broadcast.get_messages().count())
        msg = broadcast.get_messages().first()
        self.assertEquals(self.contact, msg.contact)
        self.assertEquals("What is your favorite color?", msg.text)


    def test_email_action(self):
        flow = self.flow
        sms = self.create_msg(direction=INCOMING, contact=self.contact, text="Green is my favorite")
        run = FlowRun.create(self.flow, self.contact)

        recipients = ["steve@apple.com"]
        test = EmailAction(recipients, "Subject", "Body")
        action_json = test.as_json()

        test = EmailAction.from_json(self.org, action_json)
        test.execute(run, None, sms)

        self.assertEquals(len(mail.outbox), 1)
        self.assertEquals(mail.outbox[0].subject, 'Subject')
        self.assertEquals(mail.outbox[0].body, 'Body')
        self.assertEquals(mail.outbox[0].recipients(), recipients)

        try:
            test = EmailAction([], "Subject", "Body")
            self.fail("Should have thrown due to empty recipient list")
        except FlowException as fe:
            pass

        test = EmailAction(recipients, "@contact.name added in subject", "In the body; @contact.name uses phone @contact.tel")
        action_json = test.as_json()

        test = EmailAction.from_json(self.org, action_json)
        test.execute(run, None, sms)

        self.assertEquals(len(mail.outbox), 2)
        self.assertEquals(mail.outbox[1].subject, 'Eric added in subject')
        self.assertEquals(mail.outbox[1].body, 'In the body; Eric uses phone 0788 382 382')
        self.assertEquals(mail.outbox[1].recipients(), recipients)

    def test_decimal_values(self):
        flow = self.flow
        flow.update(self.definition)

        rules = RuleSet.objects.get(uuid=uuid(5))

        # update our rule to include decimal parsing
        rules.set_rules_dict([Rule(uuid(12), "< 10", uuid(2), LtTest(10)).as_json(),
                              Rule(uuid(13), "> 10", uuid(3), GteTest(10)).as_json()])

        rules.save()

        # start the flow
        flow.start([], [self.contact])
        sms = self.create_msg(direction=INCOMING, contact=self.contact, text="My answer is 15")
        self.assertTrue(Flow.find_and_handle(sms))

        step = FlowStep.objects.get(step_uuid=uuid(5))
        self.assertEquals("> 10", step.rule_category)
        self.assertEquals(uuid(13), step.rule_uuid)
        self.assertEquals("15", step.rule_value)
        self.assertEquals(Decimal("15"), step.rule_decimal_value)

    def test_save_to_contact_action(self):
        flow = self.flow
        sms = self.create_msg(direction=INCOMING, contact=self.contact, text="batman")
        test = SaveToContactAction.from_json(self.org, dict(type='save', label="Superhero Name", value='@step'))
        run = FlowRun.create(self.flow, self.contact)

        field = ContactField.objects.get(org=self.org, key="superhero_name")
        self.assertEquals("Superhero Name", field.label)

        test.execute(run, None, sms)

        # user should now have a nickname field with a value of batman
        contact = Contact.objects.get(id=self.contact.pk)
        self.assertEquals("batman", contact.get_field_raw('superhero_name'))

        # test clearing our value
        test = SaveToContactAction.from_json(self.org, test.as_json())
        test.value = ""
        test.execute(run, None, sms)
        contact = Contact.objects.get(id=self.contact.pk)
        self.assertEquals(None, contact.get_field_raw('superhero_name'))

        # test setting our name
        test = SaveToContactAction.from_json(self.org, dict(type='save', label="Name", value='', field='name'))
        test.value = "Eric Newcomer"
        test.execute(run, None, sms)
        contact = Contact.objects.get(id=self.contact.pk)
        self.assertEquals("Eric Newcomer", contact.name)
        run.contact = contact

        # test setting just the first name
        test = SaveToContactAction.from_json(self.org, dict(type='save', label="First Name", value='', field='first_name'))
        test.value = "Jen"
        test.execute(run, None, sms)
        contact = Contact.objects.get(id=self.contact.pk)
        self.assertEquals("Jen Newcomer", contact.name)

        # we should strip whitespace
        run.contact = contact
        test = SaveToContactAction.from_json(self.org, dict(type='save', label="First Name", value='', field='first_name'))
        test.value = " Jackson "
        test.execute(run, None, sms)
        contact = Contact.objects.get(id=self.contact.pk)
        self.assertEquals("Jackson Newcomer", contact.name)

        # first name works with a single word
        run.contact = contact
        contact.name = "Percy"
        contact.save()

        test = SaveToContactAction.from_json(self.org, dict(type='save', label="First Name", value='', field='first_name'))
        test.value = " Cole"
        test.execute(run, None, sms)
        contact = Contact.objects.get(id=self.contact.pk)
        self.assertEquals("Cole", contact.name)

    def test_language_action(self):

        test = SetLanguageAction('kli', 'Kingon')

        # export and reimport
        action_json = test.as_json()
        test = SetLanguageAction.from_json(self.org, action_json)

        self.assertTrue('kli', test.lang)
        self.assertTrue('Klingon', test.lang)

        # execute our action and check we are Klingon now, eeektorp shnockahltip.
        run = FlowRun.create(self.flow, self.contact)
        test.execute(run, None, None)
        self.assertEquals('kli', Contact.objects.get(pk=self.contact.pk).language)

    def test_flow_action(self):
        orig_flow = self.create_flow()
        run = FlowRun.create(orig_flow, self.contact)

        flow = self.flow
        flow.update(self.definition)

        sms = self.create_msg(direction=INCOMING, contact=self.contact, text="Green is my favorite")

        test = StartFlowAction(flow)
        action_json = test.as_json()

        test = StartFlowAction.from_json(self.org, action_json)
        test.execute(run, None, sms, [])

        # our contact should now be in the flow
        self.assertTrue(FlowStep.objects.filter(run__flow=flow, run__contact=self.contact))
        self.assertTrue(Msg.objects.filter(contact=self.contact, direction='O', text='What is your favorite color?'))

    def test_group_actions(self):
        flow = self.flow
        sms = self.create_msg(direction=INCOMING, contact=self.contact, text="Green is my favorite")
        run = FlowRun.create(self.flow, self.contact)

        group = self.create_group("Flow Group", [])

        test = AddToGroupAction([group, "@step.contact"])
        action_json = test.as_json()
        test = AddToGroupAction.from_json(self.org, action_json)

        test.execute(run, None, sms)

        # user should now be in the group
        self.assertTrue(group.contacts.filter(id=self.contact.pk))
        self.assertEquals(1, group.contacts.all().count())

        # we should have acreated a group with the name of the contact
        replace_group = ContactGroup.objects.get(name=self.contact.name)
        self.assertTrue(replace_group.contacts.filter(id=self.contact.pk))
        self.assertEquals(1, replace_group.contacts.all().count())

        # passing through twice doesn't change anything
        test.execute(run, None, sms)

        self.assertTrue(group.contacts.filter(id=self.contact.pk))
        self.assertEquals(1, group.contacts.all().count())

        test = DeleteFromGroupAction([group, "@step.contact"])
        action_json = test.as_json()
        test = DeleteFromGroupAction.from_json(self.org, action_json)

        test.execute(run, None, sms)

        # user should be gone now
        self.assertFalse(group.contacts.filter(id=self.contact.pk))
        self.assertEquals(0, group.contacts.all().count())
        self.assertFalse(replace_group.contacts.filter(id=self.contact.pk))
        self.assertEquals(0, replace_group.contacts.all().count())

        test.execute(run, None, sms)

        self.assertFalse(group.contacts.filter(id=self.contact.pk))
        self.assertEquals(0, group.contacts.all().count())

    def test_add_label_action(self):
        flow = self.flow
        sms = self.create_msg(direction=INCOMING, contact=self.contact, text="Green is my favorite")
        run = FlowRun.create(flow, self.contact)

        label = Label.objects.create(name='green label', org=self.org)

        test = AddLabelAction([label, "@step.contact"])
        action_json = test.as_json()
        test = AddLabelAction.from_json(self.org, action_json)

        test.execute(run, None, sms)

        # sms should have been labeled
        self.assertTrue(label.get_messages())
        self.assertEquals(label.get_message_count(), 1)

        # we should have created a new label with the name of the contact
        new_label = Label.objects.get(name=self.contact.name)
        self.assertTrue(new_label.get_messages())
        self.assertEquals(new_label.get_message_count(), 1)

        # passing through twice doesn't change anything
        test.execute(run, None, sms)

        self.assertTrue(label.get_messages())
        self.assertEquals(label.get_message_count(), 1)

        self.assertTrue(new_label.get_messages())
        self.assertEquals(new_label.get_message_count(), 1)

    def test_views(self):
        self.create_secondary_org()

        # create a flow for another org
        flow2 = Flow.create(self.org2, self.admin2, "Flow2")

        # no login, no list
        response = self.client.get(reverse('flows.flow_list'))
        self.assertRedirect(response, reverse('users.user_login'))

        user = self.admin
        user.first_name = "Test"
        user.last_name = "Contact"
        user.save()
        self.login(user)

        # list, should have only one flow (the one created in setUp)
        response = self.client.get(reverse('flows.flow_list'))
        self.assertEquals(1, len(response.context['object_list']))

        # inactive list shouldn't have any flows
        response = self.client.get(reverse('flows.flow_archived'))
        self.assertEquals(0, len(response.context['object_list']))

        # also shouldn't be able to view other flow
        response = self.client.get(reverse('flows.flow_editor', args=[flow2.pk]))
        self.assertEquals(302, response.status_code)

        # get our create page
        response = self.client.get(reverse('flows.flow_create'))
        self.assertTrue(response.context['has_flows'])

        # create a new flow
        response = self.client.post(reverse('flows.flow_create'), dict(name="Flow", expires_after_minutes=5), follow=True)
        flow = Flow.objects.get(org=self.org, name="Flow")
        # add a trigger on this flow
        Trigger.objects.create(org=self.org, keyword='unique', flow=flow,
                               created_by=self.admin, modified_by=self.admin)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(5, flow.expires_after_minutes)

        # test flows with triggers

        # create a new flow with one unformatted keyword
        post_data = dict()
        post_data['name'] = "Flow With Unformated Keyword Triggers"
        post_data['keyword_triggers'] = "this is,it"
        response = self.client.post(reverse('flows.flow_create'), post_data)
        self.assertTrue(response.context['form'].errors)
        self.assertTrue('"this is" must be a single word containing only letter and numbers' in response.context['form'].errors['keyword_triggers'])

        # create a new flow with one existing keyword
        post_data = dict()
        post_data['name'] = "Flow With Existing Keyword Triggers"
        post_data['keyword_triggers'] = "this,is,unique"
        response = self.client.post(reverse('flows.flow_create'), post_data)
        self.assertTrue(response.context['form'].errors)
        self.assertTrue('The keyword "unique" is already used for another flow' in response.context['form'].errors['keyword_triggers'])

        # create a new flow with keywords
        post_data = dict()
        post_data['name'] = "Flow With Good Keyword Triggers"
        post_data['keyword_triggers'] = "this,is,it"
        post_data['expires_after_minutes'] = 30
        response = self.client.post(reverse('flows.flow_create'), post_data, follow=True)
        flow_with_keywords = Flow.objects.get(name=post_data['name'])

        self.assertEquals(200, response.status_code)
        self.assertEquals(response.request['PATH_INFO'], reverse('flows.flow_editor', args=[flow_with_keywords.pk]))
        self.assertEquals(response.context['object'].triggers.count(), 3)

        #update flow triggers
        post_data = dict()
        post_data['name'] = "Flow With Keyword Triggers"
        post_data['keyword_triggers'] = "it,changes,everything"
        post_data['expires_after_minutes'] = 60*12
        response = self.client.post(reverse('flows.flow_update', args=[flow_with_keywords.pk]), post_data, follow=True)
        flow_with_keywords = Flow.objects.get(name=post_data['name'])
        self.assertEquals(200, response.status_code)
        self.assertEquals(response.request['PATH_INFO'], reverse('flows.flow_list'))
        self.assertTrue(flow_with_keywords in response.context['object_list'].all())
        self.assertEquals(flow_with_keywords.triggers.count(), 5)
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=True).count(), 2)
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).count(), 3)
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).exclude(groups=None).count(), 0)

        # update flow with unformated keyword
        post_data['keyword_triggers'] = "it,changes,every thing"
        response = self.client.post(reverse('flows.flow_update', args=[flow_with_keywords.pk]), post_data)
        self.assertTrue(response.context['form'].errors)

        # update flow with unformated keyword
        post_data['keyword_triggers'] = "it,changes,everything,unique"
        response = self.client.post(reverse('flows.flow_update', args=[flow_with_keywords.pk]), post_data)        
        self.assertTrue(response.context['form'].errors)
        response = self.client.get(reverse('flows.flow_update', args=[flow_with_keywords.pk]))
        self.assertEquals(response.context['form'].fields['keyword_triggers'].initial, "it,everything,changes")
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).count(), 3)
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).exclude(groups=None).count(), 0)
        trigger = Trigger.objects.get(keyword="everything", flow=flow_with_keywords)
        group = self.create_group("first", [self.contact])
        trigger.groups.add(group)
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).count(), 3)
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).exclude(groups=None).count(), 1)
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).exclude(groups=None)[0].keyword, "everything")
        response = self.client.get(reverse('flows.flow_update', args=[flow_with_keywords.pk]))
        self.assertEquals(response.context['form'].fields['keyword_triggers'].initial, "it,changes")
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).count(), 3)
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).exclude(groups=None).count(), 1)
        self.assertEquals(flow_with_keywords.triggers.filter(is_archived=False).exclude(groups=None)[0].keyword, "everything")

        # create some rules on it
        self.assertEquals(0, ActionSet.objects.all().count())
        flow.update(self.definition)
        self.assertEquals(4, ActionSet.objects.all().count())

        # can see ours
        response = self.client.get(reverse('flows.flow_results', args=[flow.pk]))
        self.assertEquals(200, response.status_code)

        # list should have our one item now
        response = self.client.get(reverse('flows.flow_list'))
        self.assertEquals(3, len(response.context['object_list']))
        self.assertEquals(flow, response.context['object_list'][0])

        # start a contact on that flow
        flow.start([], [self.contact])

        # remove one of the contacts
        run = flow.runs.get(contact=self.contact)
        response = self.client.post(reverse('flows.flow_results', args=[flow.pk]), data=dict(run=run.pk))
        self.assertEquals(200, response.status_code)
        self.assertFalse(FlowStep.objects.filter(run__contact=self.contact))

        # test getting the json
        response = self.client.get(reverse('flows.flow_json', args=[flow.pk]))
        json_dict = json.loads(response.content)['flow']

        # test setting the json
        json_dict['action_sets'] = [dict(uuid=uuid(1), x=1, y=1, destination=None,
                                         actions=[dict(type='reply', msg='This flow is more like a broadcast')])]
        json_dict['rule_sets'] = []
        json_dict['entry'] = uuid(1)

        response = self.client.post(reverse('flows.flow_json', args=[flow.pk]), json.dumps(json_dict), content_type="application/json")
        self.assertEquals(200, response.status_code)
        self.assertEquals(1, ActionSet.objects.all().count())

        actionset = ActionSet.objects.get()
        self.assertEquals(actionset.flow, flow)

        # can't save with an invalid uuid
        json_dict['last_saved'] = datetime_to_str(timezone.now())
        json_dict['action_sets'][0]['destination'] = 'notthere'


        with self.assertRaises(FlowException):
            response = self.client.post(reverse('flows.flow_json', args=[flow.pk]), json.dumps(json_dict), content_type="application/json")

        # flow should still be there though
        flow = Flow.objects.get(pk=flow.pk)

        # should still have the original one, nothing changed
        response = self.client.get(reverse('flows.flow_json', args=[flow.pk]))
        self.assertEquals(200, response.status_code)
        json_dict = json.loads(response.content)

        # can't save against the other flow
        response = self.client.post(reverse('flows.flow_json', args=[flow2.pk]), json.dumps(json_dict), content_type="application/json")
        self.assertEquals(302, response.status_code)

        # can't save with invalid json
        with self.assertRaises(ValueError):
            response = self.client.post(reverse('flows.flow_json', args=[flow.pk]), "badjson", content_type="application/json")

        # test simulation
        simulate_url = reverse('flows.flow_simulate', args=[flow.pk])

        response = self.client.get(simulate_url)
        self.assertEquals(response.status_code, 302)

        post_data = dict()
        post_data['has_refresh'] = True

        response = self.client.post(simulate_url, json.dumps(post_data), content_type="application/json")
        json_dict = json.loads(response.content)

        self.assertEquals(len(json_dict.keys()), 5)
        self.assertEquals(len(json_dict['messages']), 3)
        self.assertEquals('Test Contact has entered the "Flow" flow', json_dict['messages'][0]['text'])
        self.assertEquals("This flow is more like a broadcast", json_dict['messages'][1]['text'])
        self.assertEquals("Test Contact has exited this flow", json_dict['messages'][2]['text'])

        post_data['new_message'] = "Ok, Thanks"
        post_data['has_refresh'] = False

        response = self.client.post(simulate_url, json.dumps(post_data), content_type="application/json")
        self.assertEquals(200, response.status_code)
        json_dict = json.loads(response.content)

        self.assertEquals(len(json_dict.keys()), 5)
        self.assertTrue('status' in json_dict.keys())
        self.assertTrue('visited' in json_dict.keys())
        self.assertTrue('activity' in json_dict.keys())
        self.assertTrue('messages' in json_dict.keys())
        self.assertTrue('description' in json_dict.keys())
        self.assertEquals(json_dict['status'], 'success')
        self.assertEquals(json_dict['description'], 'Message sent to Flow')

        post_data['has_refresh'] = True

        response = self.client.post(simulate_url, json.dumps(post_data), content_type="application/json")
        self.assertEquals(200, response.status_code)
        json_dict = json.loads(response.content)

        self.assertEquals(len(json_dict.keys()), 5)
        self.assertTrue('status' in json_dict.keys())
        self.assertTrue('visited' in json_dict.keys())
        self.assertTrue('activity' in json_dict.keys())
        self.assertTrue('messages' in json_dict.keys())
        self.assertTrue('description' in json_dict.keys())
        self.assertEquals(json_dict['status'], 'success')
        self.assertEquals(json_dict['description'], 'Message sent to Flow')

        # test our copy view
        response = self.client.post(reverse('flows.flow_copy', args=[flow.pk]))
        flow_copy = Flow.objects.get(org=self.org, name="Copy of %s" % flow.name)
        self.assertRedirect(response, reverse('flows.flow_editor', args=[flow_copy.pk]))

        flow_label_1 = FlowLabel.objects.create(name="one", org=self.org, parent=None)
        flow_label_2 = FlowLabel.objects.create(name="two", org=self.org2, parent=None)

        # test update view
        response = self.client.post(reverse('flows.flow_update', args=[flow.pk]))
        self.assertEquals(200, response.status_code)
        self.assertEquals(5, len(response.context['form'].fields))
        self.assertTrue('name' in response.context['form'].fields)
        self.assertTrue('keyword_triggers' in response.context['form'].fields)
        self.assertTrue('ignore_triggers' in response.context['form'].fields)

        # test broadcast view
        response = self.client.get(reverse('flows.flow_broadcast', args=[flow.pk]))
        self.assertEquals(3, len(response.context['form'].fields))
        self.assertTrue('omnibox' in response.context['form'].fields)
        self.assertTrue('restart_participants' in response.context['form'].fields)

        post_data = dict()
        post_data['omnibox'] = "c-%d" % self.contact.pk
        post_data['restart_participants'] = 'on'

        count = Broadcast.objects.all().count()
        self.client.post(reverse('flows.flow_broadcast', args=[flow.pk]), post_data, follow=True)
        self.assertEquals(count + 1, Broadcast.objects.all().count())

        # we should have a flow start
        start = FlowStart.objects.get(flow=flow)

        # should be in a completed state
        self.assertEquals(COMPLETE, start.status)
        self.assertEquals(1, start.contact_count)

        # do so again but don't restart the participants
        del post_data['restart_participants']

        self.client.post(reverse('flows.flow_broadcast', args=[flow.pk]), post_data, follow=True)

        # should have a new flow start
        new_start = FlowStart.objects.filter(flow=flow).order_by('-created_on').first()
        self.assertNotEquals(start, new_start)
        self.assertEquals(COMPLETE, new_start.status)
        self.assertEquals(0, new_start.contact_count)

        # mark that start as incomplete
        new_start.status = 'S'
        new_start.save()

        # try to start again
        response = self.client.post(reverse('flows.flow_broadcast', args=[flow.pk]), post_data, follow=True)

        # should have an error now
        self.assertTrue(response.context['form'].errors)

        # shouldn't have a new flow start as validation failed
        self.assertFalse(FlowStart.objects.filter(flow=flow).exclude(id__lte=new_start.id))

        # test creating a  flow with base language
        # create the language for our org
        language = Language.objects.create(iso_code='eng', name='English', org=self.org,
                                           created_by=flow.created_by, modified_by=flow.modified_by)
        self.org.primary_language = language
        self.org.save()

        post_data = dict(name="Language Flow", expires_after_minutes=5, base_language=language.iso_code)
        response = self.client.post(reverse('flows.flow_create'), post_data, follow=True)
        language_flow = Flow.objects.get(name=post_data['name'])

        self.assertEquals(200, response.status_code)
        self.assertEquals(response.request['PATH_INFO'], reverse('flows.flow_editor', args=[language_flow.pk]))
        self.assertEquals(language_flow.base_language, language.iso_code)

    def test_views_viewers(self):
        #create a viewer
        self.viewer= self.create_user("Viewer")
        self.org.viewers.add(self.viewer)
        self.viewer.set_org(self.org)        
        
        self.create_secondary_org()

        # create a flow for another org and a flow label
        flow2 = Flow.create(self.org2, self.admin2, "Flow2")
        flow_label = FlowLabel.objects.create(name="one", org=self.org, parent=None)

        flow_list_url = reverse('flows.flow_list')
        flow_archived_url = reverse('flows.flow_archived')
        flow_create_url = reverse('flows.flow_create')
        flowlabel_create_url = reverse('flows.flowlabel_create')

        # no login, no list
        response = self.client.get(flow_list_url)
        self.assertRedirect(response, reverse('users.user_login'))

        user = self.viewer
        user.first_name = "Test"
        user.last_name = "Contact"
        user.save()
        self.login(user)

        # list, should have only one flow (the one created in setUp)
        
        response = self.client.get(flow_list_url)
        self.assertEquals(1, len(response.context['object_list']))
        # no create links
        self.assertFalse(flow_create_url in response.content)
        self.assertFalse(flowlabel_create_url in response.content)
        # verify the action buttons we have
        self.assertFalse('object-btn-unlabel' in response.content)
        self.assertFalse('object-btn-restore' in response.content)
        self.assertFalse('object-btn-archive' in response.content)
        self.assertFalse('object-btn-label' in response.content)
        self.assertTrue('object-btn-export' in response.content)

        # can not label
        post_data = dict()
        post_data['action'] = 'label'
        post_data['objects'] = self.flow.pk
        post_data['label'] = flow_label.pk
        post_data['add'] = True

        response = self.client.post(flow_list_url, post_data, follow=True)
        self.assertEquals(1, response.context['object_list'].count())
        self.assertFalse(response.context['object_list'][0].labels.all())

        # can not archive
        post_data = dict()
        post_data['action'] = 'archive'
        post_data['objects'] = self.flow.pk
        response = self.client.post(flow_list_url, post_data, follow=True)
        self.assertEquals(1, response.context['object_list'].count())
        self.assertEquals(response.context['object_list'][0].pk, self.flow.pk)
        self.assertFalse(response.context['object_list'][0].is_archived)

        
        # inactive list shouldn't have any flows
        response = self.client.get(flow_archived_url)
        self.assertEquals(0, len(response.context['object_list']))

        response = self.client.get(reverse('flows.flow_editor', args=[self.flow.pk]))
        self.assertEquals(200, response.status_code)
        self.assertFalse(response.context['mutable'])

        # we can fetch the json for the flow
        response = self.client.get(reverse('flows.flow_json', args=[self.flow.pk]))
        self.assertEquals(200, response.status_code)

        # but posting to it should redirect to a get
        response = self.client.post(reverse('flows.flow_json', args=[self.flow.pk]), post_data=response.content)
        self.assertEquals(302, response.status_code)

        self.flow.is_archived = True
        self.flow.save()

        response = self.client.get(flow_list_url)
        self.assertEquals(0, len(response.context['object_list']))

        # can not restore
        post_data = dict()
        post_data['action'] = 'archive'
        post_data['objects'] = self.flow.pk
        response = self.client.post(flow_archived_url, post_data, follow=True)
        self.assertEquals(1, response.context['object_list'].count())
        self.assertEquals(response.context['object_list'][0].pk, self.flow.pk)
        self.assertTrue(response.context['object_list'][0].is_archived)

        response = self.client.get(flow_archived_url)
        self.assertEquals(1, len(response.context['object_list']))

        # cannot create a flow
        response = self.client.get(flow_create_url)
        self.assertEquals(302, response.status_code)

        # cannot create a flowlabel
        response = self.client.get(flowlabel_create_url)
        self.assertEquals(302, response.status_code)

        # also shouldn't be able to view other flow
        response = self.client.get(reverse('flows.flow_editor', args=[flow2.pk]))
        self.assertEquals(302, response.status_code)

    def test_multiple(self):
        # set our flow
        self.flow.update(self.definition)
        self.flow.start([], [self.contact])

        # create a second flow
        self.flow2 = Flow.create(self.org, self.admin, "Color Flow 2")

        # broadcast to one user
        self.flow2 = self.flow.copy(self.flow, self.flow.created_by)
        self.flow2.start([], [self.contact])

        # each flow should have two events
        self.assertEquals(2, FlowStep.objects.filter(run__flow=self.flow).count())
        self.assertEquals(2, FlowStep.objects.filter(run__flow=self.flow2).count())

        # send in a message
        incoming = self.create_msg(direction=INCOMING, contact=self.contact, text="Orange", created_on=timezone.now())
        self.assertTrue(Flow.find_and_handle(incoming))

        # only the second flow should get it
        self.assertEquals(2, FlowStep.objects.filter(run__flow=self.flow).count())
        self.assertEquals(3, FlowStep.objects.filter(run__flow=self.flow2).count())

        # start the flow again for our contact
        self.flow.start([], [self.contact], restart_participants=True)

        # should have two flow runs for this contact and flow
        runs = FlowRun.objects.filter(flow=self.flow, contact=self.contact).order_by('-created_on')
        self.assertTrue(runs[0].is_active)
        self.assertFalse(runs[1].is_active)

        self.assertEquals(2, runs[0].steps.all().count())
        self.assertEquals(2, runs[1].steps.all().count())

        # send in a message, this should be handled by our first flow, which has a more recent run active
        incoming = self.create_msg(direction=INCOMING, contact=self.contact, text="blue")
        self.assertTrue(Flow.find_and_handle(incoming))

        self.assertEquals(3, runs[0].steps.all().count())

        # if we exclude existing and try starting again, nothing happens
        self.flow.start([], [self.contact], restart_participants=False)

        # no new runs
        self.assertEquals(2, self.flow.runs.all().count())

        # get the results for the flow
        results = self.flow.get_results()

        # should only have one result
        self.assertEquals(1, len(results))

        # and only one value
        self.assertEquals(1, len(results[0]['values']))

        color = results[0]['values'][0]
        self.assertEquals('color', color['label'])
        self.assertEquals('Blue', color['category'])
        self.assertEquals('blue', color['value'])
        self.assertEquals(incoming.text, color['text'])


    def test_ignore_keyword_triggers(self):
        # set our flow
        self.flow.update(self.definition)
        self.flow.start([], [self.contact])

        # create a second flow
        self.flow2 = Flow.create(self.org, self.admin, "Kiva Flow")

        self.flow2 = self.flow.copy(self.flow, self.flow.created_by)

        # add a trigger on flow2
        Trigger.objects.create(org=self.org, keyword='kiva', flow=self.flow2,
                               created_by=self.admin, modified_by=self.admin)

        incoming = self.create_msg(direction=INCOMING, contact=self.contact, text="kiva")

        self.assertTrue(Trigger.find_and_handle(incoming))
        self.assertTrue(FlowRun.objects.filter(flow=self.flow2, contact=self.contact))

        self.flow.ignore_triggers = True
        self.flow.save()
        self.flow.start([], [self.contact], restart_participants=True)

        other_incoming = self.create_msg(direction=INCOMING, contact=self.contact, text="kiva")

        self.assertFalse(Trigger.find_and_handle(other_incoming))

        # complete the flow
        incoming = self.create_msg(direction=INCOMING, contact=self.contact, text="orange")
        self.assertTrue(Flow.find_and_handle(incoming))

        # now we should trigger the other flow as we are at our terminal flow
        self.assertTrue(Trigger.find_and_handle(other_incoming))


class FlowRunTest(TembaTest):

    def test_field_normalization(self):
        fields = dict(field1="value1", field2="value2")
        (normalized, count) = FlowRun.normalize_fields(fields)
        self.assertEquals(fields, normalized)

        # field text too long
        fields['field2'] = "*" * 350
        (normalized, count) = FlowRun.normalize_fields(fields)
        self.assertEquals(255, len(normalized['field2']))

        # field name too long
        fields['field' + ("*" * 350)] = "short value"
        (normalized, count) = FlowRun.normalize_fields(fields)
        self.assertTrue('field' + ("*" * 250) in normalized)

        # too many fields
        for i in range(129):
            fields['field%d' % i] = 'value %d' % i
        (normalized, count) = FlowRun.normalize_fields(fields)
        self.assertEquals(128, count)
        self.assertEquals(128, len(normalized))

        fields = dict(numbers=["zero", "one", "two", "three"])
        (normalized, count) = FlowRun.normalize_fields(fields)
        self.assertEquals(5, count)
        self.assertEquals(dict(numbers={'0': "zero", '1': "one", '2': "two", '3': "three"}), normalized)

        fields = dict(united_states=dict(wa="Washington", nv="Nevada"), states=50)
        (normalized, count) = FlowRun.normalize_fields(fields)
        self.assertEquals(4, count)
        self.assertEquals(fields, normalized)

    def test_update_fields(self):
        self.flow = self.create_flow()
        self.contact = self.create_contact("Ben Haggerty", "+250788123123")

        run = FlowRun.create(self.flow, self.contact)

        # set our fields from an empty state
        new_values = dict(field1="value1", field2="value2")
        run.update_fields(new_values)

        new_values['__default__'] = 'field1: value1, field2: value2'

        self.assertEquals(run.field_dict(), new_values)

        run.update_fields(dict(field2="new value2", field3="value3"))
        new_values['field2'] = "new value2"
        new_values['field3'] = "value3"

        new_values['__default__'] = 'field1: value1, field2: new value2, field3: value3'

        self.assertEquals(run.field_dict(), new_values)

        run.update_fields(dict(field1=""))
        new_values['field1'] = ""
        new_values['__default__'] = 'field1: , field2: new value2, field3: value3'

        self.assertEquals(run.field_dict(), new_values)

class FlowLabelTest(SmartminTest):
    def setUp(self):
        self.user = self.create_user("tito")
        self.org = Org.objects.create(name="Nyaruka Ltd.", timezone="Africa/Kigali", created_by=self.user, modified_by=self.user)
        self.org.administrators.add(self.user)
        self.user.set_org(self.org)

    def test_label_model(self):
        # test a the creation of a unique label when we have a long word(more than 32 caracters)
        response = FlowLabel.create_unique("alongwordcomposedofmorethanthirtytwoletters",
                                           self.org,
                                           parent=None)
        self.assertEquals(response.name, "alongwordcomposedofmorethanthirt")

        # try to create another label which starts with the same 32 caracteres
        # the one we already have
        label = FlowLabel.create_unique("alongwordcomposedofmorethanthirtytwocaracteres",
                                        self.org, parent=None)

        self.assertEquals(label.name, "alongwordcomposedofmorethanthi 2")
        self.assertEquals(str(label), "alongwordcomposedofmorethanthi 2")
        label = FlowLabel.create_unique("child", self.org, parent=label)
        self.assertEquals(str(label), "alongwordcomposedofmorethanthi 2 > child")

        FlowLabel.create_unique("dog", self.org)
        FlowLabel.create_unique("dog", self.org)
        dog3 = FlowLabel.create_unique("dog", self.org)
        self.assertEquals("dog 3", dog3.name)

        dog4 = FlowLabel.create_unique("dog ", self.org)
        self.assertEquals("dog 4", dog4.name)

        # view the parent label, should see the child
        self.login(self.user)
        response = self.client.get(reverse('flows.flow_filter', args=[label.pk]))
        self.assertContains(response, "child")

    def test_create(self):
        create_url = reverse('flows.flowlabel_create')

        post_data = dict(name="label_one")

        self.login(self.user)
        response = self.client.post(create_url, post_data, follow=True)
        self.assertEquals(FlowLabel.objects.all().count(), 1)
        self.assertEquals(FlowLabel.objects.all()[0].parent, None)

        label_one = FlowLabel.objects.all()[0]
        post_data = dict(name="sub_label", parent=label_one.pk)
        response = self.client.post(create_url, post_data, follow=True)

        self.assertEquals(FlowLabel.objects.all().count(), 2)
        self.assertEquals(FlowLabel.objects.filter(parent=None).count(), 1)

        post_data = dict(name="sub_label ", parent=label_one.pk)
        response = self.client.post(create_url, post_data, follow=True)
        self.assertTrue('form' in response.context)
        self.assertTrue(response.context['form'].errors)
        self.assertEquals('Name already used', response.context['form'].errors['name'][0])

        self.assertEquals(FlowLabel.objects.all().count(), 2)
        self.assertEquals(FlowLabel.objects.filter(parent=None).count(), 1)

        post_data = dict(name="label from modal")
        response = self.client.post("%s?format=modal" % create_url, post_data, follow=True)
        self.assertEquals(FlowLabel.objects.all().count(), 3)



    def test_delete(self):
        label_one = FlowLabel.create_unique("label1", self.org)

        delete_url = reverse('flows.flowlabel_delete', args=[label_one.pk])

        self.other_user = self.create_user("ironman")

        self.login(self.other_user)
        response = self.client.get(delete_url)
        self.assertEquals(response.status_code, 302)

        self.login(self.user)
        response = self.client.get(delete_url)
        self.assertEquals(response.status_code, 200)

class WebhookTest(TembaTest):

    def setUp(self):
        super(WebhookTest, self).setUp()
        settings.SEND_WEBHOOKS = True

    def tearDown(self):
        super(WebhookTest, self).tearDown()
        settings.SEND_WEBHOOKS = False

    def test_webhook(self):
        self.flow = self.create_flow()
        self.contact = self.create_contact("Ben Haggerty", '+250788383383')

        run = FlowRun.create(self.flow, self.contact)

        rules = RuleSet.objects.create(flow=self.flow, uuid=uuid(100), x=0, y=0)
        rules.set_rules_dict([Rule(uuid(12), "Valid", uuid(2), ContainsTest("valid")).as_json(),
                              Rule(uuid(13), "Invalid", uuid(3), ContainsTest("invalid")).as_json()])
        rules.save()

        step = FlowStep.objects.create(run=run, contact=run.contact, step_type=RULE_SET, step_uuid=rules.uuid)
        incoming = self.create_msg(direction=INCOMING, contact=self.contact, text="1001")

        (match, value) = rules.find_matching_rule(step, run, incoming)
        self.assertIsNone(match)
        self.assertIsNone(value)

        rules.webhook_url = "http://ordercheck.com/check_order.php?phone=@step.contact.tel_e164"
        rules.webhook_action = "GET"
        rules.operand = "@extra.text @extra.blank"
        rules.save()

        with patch('requests.get') as get:
            with patch('requests.post') as post:
                get.return_value = MockResponse(200, '{ "text": "Get", "blank": "" }')
                post.return_value = MockResponse(200, '{ "text": "Post", "blank": "" }')

                # first do a GET
                rules.find_matching_rule(step, run, incoming)
                self.assertEquals(dict(__default__='blank: , text: Get', text="Get", blank=""), run.field_dict())

                # assert our phone number got encoded
                self.assertEquals("http://ordercheck.com/check_order.php?phone=%2B250788383383", get.call_args[0][0])

                # now do a POST
                rules.webhook_action = "POST"
                rules.save()
                rules.find_matching_rule(step, run, incoming)
                self.assertEquals(dict(__default__='blank: , text: Post', text="Post", blank=""), run.field_dict())

                self.assertEquals("http://ordercheck.com/check_order.php?phone=%2B250788383383", post.call_args[0][0])

        # remove @extra.blank from our text
        rules.operand = "@extra.text"
        rules.save()

        # clear our run's field dict
        run.fields = json.dumps(dict())
        run.save()

        with patch('requests.post') as mock:
            mock.return_value = MockResponse(200, '{ "text": "Valid" }')

            (match, value) = rules.find_matching_rule(step, run, incoming)

            self.assertEquals(uuid(12), match.uuid)
            self.assertEquals("Valid", value)
            self.assertEquals(dict(__default__='text: Valid', text="Valid"), run.field_dict())

        with patch('requests.post') as mock:
            mock.return_value = MockResponse(200, '{ "text": "Valid", "order_number": "PX1001" }')

            (match, value) = rules.find_matching_rule(step, run, incoming)
            self.assertEquals(uuid(12), match.uuid)
            self.assertEquals("Valid", value)
            self.assertEquals(dict(__default__='order_number: PX1001, text: Valid', text="Valid", order_number="PX1001"), run.field_dict())

            message_context = self.flow.build_message_context(self.contact, incoming)
            self.assertEquals(dict(text="Valid", order_number="PX1001", __default__='order_number: PX1001, text: Valid'), message_context['extra'])

        with patch('requests.post') as mock:
            mock.return_value = MockResponse(200, '{ "text": "Valid", "order_number": "PX1002" }')

            (match, value) = rules.find_matching_rule(step, run, incoming)
            self.assertEquals(uuid(12), match.uuid)
            self.assertEquals("Valid", value)
            self.assertEquals(dict(__default__='order_number: PX1002, text: Valid', text="Valid", order_number="PX1002"), run.field_dict())

            message_context = self.flow.build_message_context(self.contact, incoming)
            self.assertEquals(dict(text="Valid", order_number="PX1002", __default__='order_number: PX1002, text: Valid'), message_context['extra'])

        with patch('requests.post') as mock:
            mock.return_value = MockResponse(200, "asdfasdfasdf")
            step.run.fields = None
            step.run.save()

            (match, value) = rules.find_matching_rule(step, run, incoming)
            self.assertIsNone(match)
            self.assertIsNone(value)
            self.assertEquals("1001", incoming.text)

            message_context = self.flow.build_message_context(self.contact, incoming)
            self.assertEquals({'__default__': ''}, message_context['extra'])

        with patch('requests.post') as mock:
            mock.return_value = MockResponse(200, "12345")
            step.run.fields = None
            step.run.save()

            (match, value) = rules.find_matching_rule(step, run, incoming)
            self.assertIsNone(match)
            self.assertIsNone(value)
            self.assertEquals("1001", incoming.text)

            message_context = self.flow.build_message_context(self.contact, incoming)
            self.assertEquals({'__default__': ''}, message_context['extra'])

        with patch('requests.post') as mock:
            mock.return_value = MockResponse(500, "Server Error")
            step.run.fields = None
            step.run.save()

            (match, value) = rules.find_matching_rule(step, run, incoming)
            self.assertIsNone(match)
            self.assertIsNone(value)
            self.assertEquals("1001", incoming.text)

class SimulationTest(FlowFileTest):

    def test_simulation(self):
        flow = self.get_flow('pick_a_number')

        # remove our channels
        self.org.channels.all().delete()

        simulate_url = reverse('flows.flow_simulate', args=[flow.pk])
        self.admin.first_name = "Ben"
        self.admin.last_name = "Haggerty"
        self.admin.save()

        post_data = dict()
        post_data['has_refresh'] = True

        self.login(self.admin)
        response = self.client.post(simulate_url, json.dumps(post_data), content_type="application/json")
        json_dict = json.loads(response.content)

        self.assertEquals(len(json_dict.keys()), 5)
        self.assertEquals(len(json_dict['messages']), 2)
        self.assertEquals('Ben Haggerty has entered the "pick_a_number" flow', json_dict['messages'][0]['text'])
        self.assertEquals("Pick a number between 1-10.", json_dict['messages'][1]['text'])

        post_data['new_message'] = "3"
        post_data['has_refresh'] = False

        response = self.client.post(simulate_url, json.dumps(post_data), content_type="application/json")
        self.assertEquals(200, response.status_code)
        json_dict = json.loads(response.content)

        self.assertEquals(len(json_dict['messages']), 5)
        self.assertEquals("3", json_dict['messages'][2]['text'])
        self.assertEquals("You picked 3!", json_dict['messages'][3]['text'])
        self.assertEquals('Ben Haggerty has exited this flow', json_dict['messages'][4]['text'])


class FlowsTest(FlowFileTest):

    def clear_activity(self, flow):
        r = get_redis_connection()
        flow.clear_cache()

    def test_activity(self):

        flow = self.get_flow('favorites')

        # clear our previous redis activity
        self.clear_activity(flow)

        other_rule_to_msg = 'e342d6af-7149-485c-b2ac-0e56c6cc1aa9:dcd9541a-0263-474e-b3f1-03a28993f95a'
        msg_to_color_step = 'dcd9541a-0263-474e-b3f1-03a28993f95a:1a08ec37-2218-48fd-b6b0-846b14407041'

        # we don't know this shade of green, it should route us to the beginning again
        self.send_message(flow, 'chartreuse')
        (active, visited) = flow.get_activity()
        self.assertEquals(1, len(active))
        self.assertEquals(1, active['1a08ec37-2218-48fd-b6b0-846b14407041'])
        self.assertEquals(1, visited[other_rule_to_msg])
        self.assertEquals(1, visited[msg_to_color_step])
        self.assertEquals(1, flow.get_total_runs())
        self.assertEquals(1, flow.get_total_contacts())
        self.assertEquals(0, flow.get_completed_runs())
        self.assertEquals(0, flow.get_completed_percentage())

        # another unknown color, that'll route us right back again
        # the active stats will look the same, but there should be one more journey on the path
        self.send_message(flow, 'mauve')
        (active, visited) = flow.get_activity()
        self.assertEquals(1, len(active))
        self.assertEquals(1, active['1a08ec37-2218-48fd-b6b0-846b14407041'])
        self.assertEquals(2, visited[other_rule_to_msg])
        self.assertEquals(2, visited[msg_to_color_step])

        # this time a color we know takes us elsewhere, activity will move
        # to another node, but still just one entry
        self.send_message(flow, 'blue')
        (active, visited) = flow.get_activity()
        self.assertEquals(1, len(active))
        self.assertEquals(1, active['0784d7f8-3534-4432-99ad-7e4ea41cfbdb'])

        # a new participant, showing distinct active counts and incremented path
        ryan = self.create_contact('Ryan Lewis', '+12065550725')
        self.send_message(flow, 'burnt sienna', contact=ryan)
        (active, visited) = flow.get_activity()
        self.assertEquals(2, len(active))
        self.assertEquals(1, active['1a08ec37-2218-48fd-b6b0-846b14407041'])
        self.assertEquals(1, active['0784d7f8-3534-4432-99ad-7e4ea41cfbdb'])
        self.assertEquals(3, visited[other_rule_to_msg])
        self.assertEquals(3, visited[msg_to_color_step])
        self.assertEquals(2, flow.get_total_runs())
        self.assertEquals(2, flow.get_total_contacts())

        # now let's have them land in the same place
        self.send_message(flow, 'blue', contact=ryan)
        (active, visited) = flow.get_activity()
        self.assertEquals(1, len(active))
        self.assertEquals(2, active['0784d7f8-3534-4432-99ad-7e4ea41cfbdb'])

        # now move our first contact forward to the end, back to two nodes with active
        self.send_message(flow, 'Turbo King')
        self.send_message(flow, 'Ben Haggerty')
        (active, visited) = flow.get_activity()
        self.assertEquals(2, len(active))

        # half of our flows are now complete
        self.assertEquals(1, flow.get_completed_runs())
        self.assertEquals(50, flow.get_completed_percentage())

        # rebuild our flow stats and make sure they are the same
        flow.do_calculate_flow_stats()
        (active, visited) = flow.get_activity()
        self.assertEquals(2, len(active))
        self.assertEquals(3, visited[other_rule_to_msg])
        self.assertEquals(1, flow.get_completed_runs())
        self.assertEquals(50, flow.get_completed_percentage())

        # expire the first contact's runs
        for run in FlowRun.objects.filter(contact=self.contact):
            run.expire()

        # now we should only have one node with active runs, but the paths stay
        # the same since those are historical
        (active, visited) = flow.get_activity()
        self.assertEquals(1, len(active))
        self.assertEquals(3, visited[other_rule_to_msg])

        # our completion stats should remain the same
        self.assertEquals(1, flow.get_completed_runs())
        self.assertEquals(50, flow.get_completed_percentage())


        # our completion stats should remain the same
        self.assertEquals(1, flow.get_completed_runs())
        self.assertEquals(50, flow.get_completed_percentage())

        # check that we have the right number of steps and runs
        self.assertEquals(17, FlowStep.objects.all().count())
        self.assertEquals(2, FlowRun.objects.all().count())

        # now let's delete our contact, we'll still have one active node, but
        # our visit path counts will go down by two since he went there twice
        self.contact.release()
        (active, visited) = flow.get_activity()
        self.assertEquals(1, len(active))
        self.assertEquals(1, visited[msg_to_color_step])
        self.assertEquals(1, visited[other_rule_to_msg])
        self.assertEquals(1, flow.get_total_runs())
        self.assertEquals(1, flow.get_total_contacts())

        # he was also accounting for our completion rate, back to nothing
        self.assertEquals(0, flow.get_completed_runs())
        self.assertEquals(0, flow.get_completed_percentage())

        # advance ryan to the end to make sure our percentage accounts for one less contact
        self.send_message(flow, 'Turbo King', contact=ryan)
        self.send_message(flow, 'Ryan Lewis', contact=ryan)
        self.assertEquals(1, flow.get_completed_runs())
        self.assertEquals(100, flow.get_completed_percentage())

        # test contacts should not affect the counts
        hammer = self.create_contact('Hammer', '+12065550002')
        hammer.is_test = True
        hammer.save()

        # please hammer, don't hurt em
        self.send_message(flow, 'Rose', contact=hammer)
        self.send_message(flow, 'Violet', contact=hammer)
        self.send_message(flow, 'Blue', contact=hammer)
        self.send_message(flow, 'Turbo King', contact=hammer)
        self.send_message(flow, 'MC Hammer', contact=hammer)

        # our flow stats should be unchanged
        (active, visited) = flow.get_activity()
        self.assertEquals(1, len(active))
        self.assertEquals(1, visited[msg_to_color_step])
        self.assertEquals(1, visited[other_rule_to_msg])
        self.assertEquals(1, flow.get_total_runs())
        self.assertEquals(1, flow.get_total_contacts())
        self.assertEquals(1, flow.get_completed_runs())
        self.assertEquals(100, flow.get_completed_percentage())

        # but hammer should have created some simulation activity
        (active, visited) = flow.get_activity(simulation=True)
        self.assertEquals(1, len(active))
        self.assertEquals(2, visited[msg_to_color_step])
        self.assertEquals(2, visited[other_rule_to_msg])

        # delete our last contact to make sure activity is gone without first expiring, zeros abound
        ryan.release()
        (active, visited) = flow.get_activity()
        self.assertEquals(0, len(active))
        self.assertEquals(0, visited[msg_to_color_step])
        self.assertEquals(0, visited[other_rule_to_msg])
        self.assertEquals(0, flow.get_total_runs())
        self.assertEquals(0, flow.get_total_contacts())
        self.assertEquals(0, flow.get_completed_runs())
        self.assertEquals(0, flow.get_completed_percentage())

        # runs and steps all gone too
        self.assertEquals(0, FlowStep.objects.filter(contact__is_test=False).count())
        self.assertEquals(0, FlowRun.objects.filter(contact__is_test=False).count())

    def test_decimal_substitution(self):
        flow = self.get_flow('pick_a_number')
        self.assertEquals("You picked 3!", self.send_message(flow, "3"))

    def test_rules_first(self):
        flow = self.get_flow('rules_first')
        self.assertEquals(Flow.RULES_ENTRY, flow.entry_type)
        self.assertEquals("You've got to be kitten me", self.send_message(flow, "cats"))

    def test_substitution(self):
        flow = self.get_flow('substitution')
        self.assertEquals("Thanks, you typed +250788123123", self.send_message(flow, "0788123123"))
        sms = Msg.objects.get(org=flow.org, contact__urns__path="+250788123123")
        self.assertEquals("Hi from Ben Haggerty! Your phone is 0788 123 123.", sms.text)

    def test_new_contact(self):
        mother_flow = self.get_flow('mama_mother_registration')
        registration_flow = self.get_flow('mama_registration', dict(NEW_MOTHER_FLOW_ID=mother_flow.pk))

        self.assertEquals("Enter the expected delivery date.", self.send_message(registration_flow, "Judy Pottier"))
        self.assertEquals("Great, thanks for registering the new mother", self.send_message(registration_flow, "31.1.2015"))

        mother = Contact.objects.get(org=self.org, name="Judy Pottier")
        self.assertTrue(mother.get_field_raw('edd').startswith('31-01-2015'))
        self.assertEquals(mother.get_field_raw('chw_phone'), self.contact.get_urn(TEL_SCHEME).path)
        self.assertEquals(mother.get_field_raw('chw_name'), self.contact.name)

    def test_group_rule_first(self):
        rule_flow = self.get_flow('group_rule_first')

        # start our contact down it
        rule_flow.start([], [self.contact], restart_participants=True)

        # contact should get a message that they didn't match either group
        self.assertLastResponse("You are something else.")

        # add them to the father's group
        self.create_group("Fathers", [self.contact])

        rule_flow.start([], [self.contact], restart_participants=True)
        self.assertLastResponse("You are a father.")

    def test_mother_registration(self):
        mother_flow = self.get_flow('new_mother')
        registration_flow = self.get_flow('mother_registration', dict(NEW_MOTHER_FLOW_ID=mother_flow.pk))

        self.assertEquals("What is her expected delivery date?", self.send_message(registration_flow, "Judy Pottier"))
        self.assertEquals("What is her phone number?", self.send_message(registration_flow, "31.1.2014"))
        self.assertEquals("Great, you've registered the new mother!", self.send_message(registration_flow, "0788 383 383"))

        mother = Contact.from_urn(self.org, TEL_SCHEME, "+250788383383")
        self.assertEquals("Judy Pottier", mother.name)
        self.assertTrue(mother.get_field_raw('expected_delivery_date').startswith('31-01-2014'))
        self.assertEquals("+12065552020", mother.get_field_raw('chw'))
        self.assertTrue(mother.groups.filter(name="Expecting Mothers"))

        pain_flow = self.get_flow('pain_flow')
        self.assertEquals("Your CHW will be in contact soon!", self.send_message(pain_flow, "yes", contact=mother))

        chw = self.contact
        sms = Msg.objects.filter(contact=chw).order_by('-created_on')[0]
        self.assertEquals("Please follow up with Judy Pottier, she has reported she is in pain.", sms.text)

    def test_flow_export_results(self):
        mother_flow = self.get_flow('new_mother')
        registration_flow = self.get_flow('mother_registration', dict(NEW_MOTHER_FLOW_ID=mother_flow.pk))

        # start our test contact down the flow
        self.assertEquals("What is her expected delivery date?",
                          self.send_message(registration_flow, "Test Mother", contact=Contact.get_test_contact(self.admin)))

        # then a real contact
        self.assertEquals("What is her expected delivery date?", self.send_message(registration_flow, "Judy Pottier"))
        self.assertEquals("That doesn't look like a valid date, try again.", self.send_message(registration_flow, "NO"))
        self.assertEquals("What is her phone number?", self.send_message(registration_flow, "31.1.2014"))
        self.assertEquals("Great, you've registered the new mother!", self.send_message(registration_flow, "0788 383 383"))

        # export the flow
        task = ExportFlowResultsTask.objects.create(created_by=self.admin, modified_by=self.admin)
        task.flows.add(registration_flow)
        task.do_export()

        task = ExportFlowResultsTask.objects.get(pk=task.id)

        # read it back in, check values
        from xlrd import open_workbook
        workbook = open_workbook(os.path.join(settings.MEDIA_ROOT, task.filename), 'rb')

        self.assertEquals(3, len(workbook.sheets()))
        entries = workbook.sheets()[0]
        self.assertEquals(2, entries.nrows)
        self.assertEquals(14, entries.ncols)

        # make sure our date hour is correct in our current timezone, we only look at hour as that
        # is what changes per timezone
        org_timezone = pytz.timezone(self.org.timezone)
        org_now = timezone.now().astimezone(org_timezone)
        self.assertEquals(org_now.hour, xldate_as_tuple(entries.cell(1, 4).value, 0)[3])

        # name category and value and raw
        self.assertEquals("All Responses", entries.cell(1, 5).value)
        self.assertEquals("Judy Pottier", entries.cell(1, 6).value)
        self.assertEquals("Judy Pottier", entries.cell(1, 7).value)

        # EDD category and value and raw
        self.assertEquals("is a date", entries.cell(1, 8).value)
        self.assertTrue(entries.cell(1, 9).value.startswith("31-01-2014"))
        self.assertEquals("31.1.2014", entries.cell(1, 10).value)

        # Phone category and value and raw
        self.assertEquals("phone", entries.cell(1, 11).value)
        self.assertEquals("+250788383383", entries.cell(1, 12).value)
        self.assertEquals("0788 383 383", entries.cell(1, 13).value)

        messages = workbook.sheets()[2]
        self.assertEquals(10, messages.nrows)
        self.assertEquals(5, messages.ncols)

        # assert the time is correct here as well
        self.assertEquals(org_now.hour, xldate_as_tuple(entries.cell(1, 3).value, 0)[3])

    def test_flow_export(self):
        flow = self.get_flow('favorites')

        # now let's export it
        self.login(self.admin)
        response = self.client.get(reverse('flows.flow_export', args=[flow.pk]))
        modified_on = flow.modified_on
        self.assertEquals(200, response.status_code)

        definition = json.loads(response.content)
        self.assertEquals(4, definition.get('version', 0))
        self.assertEquals(1, len(definition.get('flows', [])))

        # try importing it and see that we have an updated flow
        Flow.import_flows(definition, self.org, self.admin)
        flow = Flow.objects.filter(name="%s" % flow.name).first()
        self.assertIsNotNone(flow)
        self.assertNotEqual(modified_on, flow.modified_on)

        # don't allow exports that reference other flows
        new_mother = self.get_flow('new_mother')
        flow = self.get_flow('references_other_flows',
                             substitutions=dict(START_FLOW=new_mother.pk,
                                                TRIGGER_FLOW=self.get_flow('pain_flow').pk))
        response = self.client.get(reverse('flows.flow_export', args=[flow.pk]))
        self.assertContains(response, "Sorry, this flow cannot be exported")
        self.assertContains(response, "new_mother")
        self.assertContains(response, "pain_flow")

        # now try importing it into a completey different org
        trey = self.create_user("Trey Anastasio")
        trey_org = Org.objects.create(name="Gotta Jiboo", timezone="Africa/Kigali", created_by=trey, modified_by=trey)
        trey_org.administrators.add(trey)

        response = self.client.get(reverse('flows.flow_export', args=[new_mother.pk]))
        definition = json.loads(response.content)

        Flow.import_flows(definition, trey_org, trey)
        self.assertIsNotNone(Flow.objects.filter(org=trey_org, name="new_mother").first())

    def test_different_expiration(self):
        flow = self.get_flow('favorites')
        self.send_message(flow, "RED", restart_participants=True)

        # get the latest run
        first_run = flow.runs.all()[0]
        first_expires = first_run.expires_on

        # start it again
        self.send_message(flow, "RED", restart_participants=True)

        # previous run should no longer be active
        first_run = FlowRun.objects.get(pk=first_run.pk)
        self.assertFalse(first_run.is_active)

        # expires on shouldn't have changed on it though
        self.assertEquals(first_expires, first_run.expires_on)

        # new run should have a different expires on
        new_run = flow.runs.all()[1]
        self.assertTrue(new_run.expires_on != first_run.expires_on)

    def test_flow_expiration(self):
        flow = self.get_flow('favorites')
        self.assertEquals("Good choice, I like Red too! What is your favorite beer?", self.send_message(flow, "RED"))
        self.assertEquals("Mmmmm... delicious Turbo King. If only they made red Turbo King! Lastly, what is your name?", self.send_message(flow, "turbo"))
        self.assertEquals(1, flow.runs.count())

        # pretend our step happened 10 minutes ago
        step = FlowStep.objects.filter(run=flow.runs.all()[0], left_on=None)[0]
        step.arrived_on = timezone.now() - timedelta(minutes=10)
        step.save()

        # now let's expire them out of the flow prematurely
        flow.expires_after_minutes = 5
        flow.save()

        # this normally gets run on FlowCRUDL.Update
        flow.update_run_expirations()

        # check that our run is expired
        run = flow.runs.all()[0]
        self.assertFalse(run.is_active)

        # we will be starting a new run now, since the other expired
        self.assertEquals("I don't know that color. Try again.", self.send_message(flow, "Michael Jordan"))
        self.assertEquals(2, flow.runs.count())

    def test_parsing(self):

        # test a preprocess url
        flow = self.get_flow('preprocess')
        self.assertEquals('http://preprocessor.com/endpoint.php', flow.rule_sets.all()[0].webhook_url)

        # now update to one without a preprocess url and make sure it disappears
        flow = self.update_flow(flow, 'pick_a_number')
        self.assertIsNone(flow.rule_sets.all()[0].webhook_url)

    def test_flow_loops(self):
        # this tests two flows that start each other
        flow1 = self.create_flow()
        flow2 = self.create_flow()

        # create an action on flow1 to start flow2
        flow1.update(dict(action_sets=[dict(uuid=uuid(1), x=1, y=1,
                                            actions=[dict(type='flow', id=flow2.pk)])]))
        flow2.update(dict(action_sets=[dict(uuid=uuid(2), x=1, y=1,
                                            actions=[dict(type='flow', id=flow1.pk)])]))

        # start the flow, shouldn't get into a loop, but both should get started
        flow1.start([], [self.contact])

        self.assertTrue(FlowRun.objects.get(flow=flow1, contact=self.contact))
        self.assertTrue(FlowRun.objects.get(flow=flow2, contact=self.contact))

    def test_parent_child(self):
        from temba.campaigns.models import Campaign, CampaignEvent, EventFire

        favorites = self.get_flow('favorites')

        # do a dry run once so that the groups and fields get created
        group = self.create_group("Campaign", [])
        field = ContactField.get_or_create(self.org, "campaign_date", "Campaign Date")

        # tests that a contact is properly updated when a child flow is called
        child = self.get_flow('child')
        parent = self.get_flow('parent', dict(CHILD_ID=child.id))

        # create a campaign with a single event
        campaign = Campaign.objects.create(name="Test Campaign", group=group, org=self.org,
                                           created_by=self.admin, modified_by=self.admin)
        CampaignEvent.objects.create(campaign=campaign, flow=favorites, relative_to=field,
                                     offset=10, unit='W', created_by=self.admin, modified_by=self.admin)

        self.assertEquals("Added to campaign.", self.send_message(parent, "start", initiate_flow=True))

        # should have one event scheduled for this contact
        self.assertTrue(EventFire.objects.filter(contact=self.contact))

    def test_tanslations_rule_first(self):

        # import a rule first flow that already has language dicts
        # this rule first does not depend on @step.value for the first rule, so
        # it can be evaluated right away
        flow = self.get_flow('group_membership')

        # create the language for our org
        language = Language.objects.create(iso_code='eng', name='English', org=self.org,
                                           created_by=flow.created_by, modified_by=flow.modified_by)
        self.org.primary_language = language
        self.org.save()

        # start our flow without a message (simulating it being fired by a trigger or the simulator)
        # this will evaluate requires_step() to make sure it handles localized flows
        runs = flow.start_msg_flow([self.contact])
        self.assertEquals(1, len(runs))
        self.assertEquals(1, self.contact.msgs.all().count())
        self.assertEquals('You are not in the enrolled group.', self.contact.msgs.all()[0].text)

        enrolled_group = ContactGroup.create(self.org, self.user, "Enrolled")
        enrolled_group.update_contacts([self.contact], True)

        runs_started = flow.start_msg_flow([self.contact])
        self.assertEquals(1, len(runs_started))
        self.assertEquals(2, self.contact.msgs.all().count())
        self.assertEquals('You are in the enrolled group.', self.contact.msgs.all().order_by('-pk')[0].text)

    def test_translations(self):

        favorites = self.get_flow('favorites')

        # create a new language on the org
        language = Language.objects.create(iso_code='eng', name='English', org=self.org,
                                           created_by=favorites.created_by, modified_by=favorites.modified_by)

        # set it as our primary language
        self.org.primary_language = language
        self.org.save()

        # everything should work as normal with our flow
        self.assertEquals("What is your favorite color?", self.send_message(favorites, "favorites", initiate_flow=True))
        json_dict = favorites.as_json()
        reply = json_dict['action_sets'][0]['actions'][0]

        # we should be a normal unicode response
        self.assertTrue(isinstance(reply['msg'], unicode))

        # now update our flow to use it
        favorites.base_language = language.iso_code
        favorites.save()
        favorites.update_base_language()

        # now our replies are language dicts
        json_dict = favorites.as_json()
        reply = json_dict['action_sets'][1]['actions'][0]
        self.assertTrue(isinstance(reply['msg'], dict))
        self.assertEquals('Good choice, I like @flow.color.category too! What is your favorite beer?', reply['msg']['eng'])

        # now interact with the flow and make sure we get an appropriate resonse
        FlowRun.objects.all().delete()

        self.assertEquals("What is your favorite color?", self.send_message(favorites, "favorites", initiate_flow=True))
        self.assertEquals("Good choice, I like Red too! What is your favorite beer?", self.send_message(favorites, "RED"))

        # now let's add a second language
        Language.objects.create(iso_code='kli', name='Klingon', org=self.org,
                                created_by=favorites.created_by, modified_by=favorites.modified_by)

        # update our initial message
        initial_message = json_dict['action_sets'][0]['actions'][0]
        initial_message['msg']['kli'] = 'Kikshtik derklop?'
        json_dict['action_sets'][0]['actions'][0] = initial_message

        # and the first response
        reply['msg']['kli'] = 'Katishklick Shnik @flow.color.category Errrrrrrrklop'
        json_dict['action_sets'][1]['actions'][0] = reply

        # save the changes
        self.assertEquals('success', favorites.update(json_dict, self.admin)['status'])

        # should get org primary language (english) since our contact has no preferred language
        FlowRun.objects.all().delete()
        self.assertEquals("What is your favorite color?", self.send_message(favorites, "favorite", initiate_flow=True))
        self.assertEquals("Good choice, I like Red too! What is your favorite beer?", self.send_message(favorites, "RED"))

        # now set our contact's preferred language to klingon
        FlowRun.objects.all().delete()
        self.contact.language = 'kli'
        self.contact.save()

        self.assertEquals("Kikshtik derklop?", self.send_message(favorites, "favorite", initiate_flow=True))
        self.assertEquals("Katishklick Shnik Red Errrrrrrrklop", self.send_message(favorites, "RED"))

        # we support localized rules and categories as well
        json_dict = favorites.as_json()
        rule = json_dict['rule_sets'][0]['rules'][0]
        self.assertTrue(isinstance(rule['test']['test'], dict))
        rule['test']['test']['kli'] = 'klerk'
        rule['category']['kli'] = 'Klerkistikloperopikshtop'
        json_dict['rule_sets'][0]['rules'][0] = rule
        self.assertEquals('success', favorites.update(json_dict, self.admin)['status'])

        FlowRun.objects.all().delete()
        self.assertEquals("Katishklick Shnik Klerkistikloperopikshtop Errrrrrrrklop", self.send_message(favorites, "klerk"))

        # test the send action as well
        json_dict = favorites.as_json()
        action = json_dict['action_sets'][1]['actions'][0]
        action['type'] = 'send'
        action['contacts'] = [dict(id=self.contact.pk)]
        action['groups'] = []
        action['variables'] = []
        json_dict['action_sets'][1]['actions'][0] = action
        self.assertEquals('success', favorites.update(json_dict, self.admin)['status'])

        FlowRun.objects.all().delete()
        self.send_message(favorites, "klerk", assert_reply=False)
        sms = Msg.objects.filter(contact=self.contact).order_by('-pk')[0]
        self.assertEquals("Katishklick Shnik Klerkistikloperopikshtop Errrrrrrrklop", sms.text)

        # test dirty json
        json_dict = favorites.as_json()

        # boolean values in our language dict shouldn't blow up
        json_dict['action_sets'][0]['actions'][0]['msg']['updated'] = True
        json_dict['action_sets'][0]['actions'][0]['msg']['kli'] = 'Bleck'

        # boolean values in our rule dict shouldn't blow up
        rule = json_dict['rule_sets'][0]['rules'][0]
        rule['category']['updated'] = True

        response = favorites.update(json_dict)
        self.assertEquals('success', response['status'])

        favorites = Flow.objects.get(pk=favorites.pk)
        json_dict = favorites.as_json()
        action = self.assertEquals('Bleck', json_dict['action_sets'][0]['actions'][0]['msg']['kli'])

        # test that simulation takes language into account
        self.login(self.admin)
        simulate_url = reverse('flows.flow_simulate', args=[favorites.pk])
        response = json.loads(self.client.post(simulate_url, json.dumps(dict(has_refresh=True)), content_type="application/json").content)
        self.assertEquals('What is your favorite color?', response['messages'][1]['text'])

        # now lets toggle the UI to Klingon and try the same thing
        simulate_url = "%s?lang=kli" % reverse('flows.flow_simulate', args=[favorites.pk])
        response = json.loads(self.client.post(simulate_url, json.dumps(dict(has_refresh=True)), content_type="application/json").content)
        self.assertEquals('Bleck', response['messages'][1]['text'])



class DuplicateValueTest(FlowFileTest):

    def test_duplicate_value_test(self):
        flow = self.get_flow('favorites')

        self.assertEquals("I don't know that color. Try again.", self.send_message(flow, "carpet"))

        # get the run for our contact
        run = FlowRun.objects.get(contact=self.contact, flow=flow)

        # we should have one value for this run, "Other"
        value = Value.objects.get(run=run)
        self.assertEquals("Other", value.category)

        # retry with "red" as an aswer
        self.assertEquals("Good choice, I like Red too! What is your favorite beer?", self.send_message(flow, "red"))

        # we should now still have only one value, but the category should be Red now
        value = Value.objects.get(run=run)
        self.assertEquals("Red", value.category)

class WebhookLoopTest(FlowFileTest):

    def setUp(self):
        super(WebhookLoopTest, self).setUp()
        settings.SEND_WEBHOOKS = True

    def tearDown(self):
        super(WebhookLoopTest, self).tearDown()
        settings.SEND_WEBHOOKS = False

    def test_webhook_loop(self):
        flow = self.get_flow('webhook_loop')

        with patch('requests.get') as mock:
            mock.return_value = MockResponse(200, '{ "text": "first message" }')
            self.assertEquals("first message", self.send_message(flow, "first", initiate_flow=True))

        with patch('requests.get') as mock:
            mock.return_value = MockResponse(200, '{ "text": "second message" }')
            self.assertEquals("second message", self.send_message(flow, "second"))
