"""Queue behavior checks; no database, files, or mail are touched."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    'feedback_email', Path(__file__).resolve().parents[1] / 'feedback_email.py')
feedback = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'psycopg2': types.ModuleType('psycopg2')}):
    spec.loader.exec_module(feedback)


class FeedbackQueueTests(unittest.TestCase):
    def row(self, app=True, email='customer@example.com', start=None):
        return (email, 'SIM1', 'ORDER1', 'customer', 'tsim.in',
                start or date.today() - timedelta(days=40),
                'japan-7d-2gbpd-esim', 'Customer', app, True)

    def run_process(self, rows, dry_run=False, insert_results=None):
        inserts = []
        logs = []
        results = iter(insert_results or [1] * len(rows))

        def query(sql, params=None):
            if sql == feedback.CUSTOMER_SELECT_QUERY:
                return rows
            if sql == feedback.NOTIFICATION_INSERT_QUERY:
                inserts.append(params)
                return next(results)
            return []  # Exercise template fallbacks.

        with patch.multiple(feedback, queryDB=Mock(side_effect=query),
                            write_log=Mock(side_effect=lambda message, *args: logs.append(message)),
                            read_exception_list=Mock(return_value={'excluded@example.com'}),
                            add_to_exception_list=Mock(), send_summary_email=Mock(),
                            DRY_RUN=dry_run):
            feedback.process_notifications()
            summary = feedback.send_summary_email.call_args
            exceptions = feedback.add_to_exception_list.call_args_list
        self.assertFalse(any('FATAL' in message or 'unexpected error' in message for message in logs), logs)
        return inserts, summary.args, exceptions, logs

    def test_app_user_queues_aligned_email_and_push(self):
        inserts, summary, exceptions, _ = self.run_process([self.row()])
        item = inserts[0]
        self.assertEqual(item['notification_type'], ['email', 'push'])
        self.assertEqual(item['status'], ['email_pending', 'push_pending'])
        for key in ('subject_template', 'message_template', 'subject_content', 'message_content'):
            self.assertEqual(len(item[key]), 2)
        self.assertEqual(item['message_template'][1], feedback.FALLBACK_PUSH_MESSAGE)
        self.assertIn('App Store or Play Store', item['message_template'][1])
        self.assertNotIn('<review_link>', item['message_template'][1])
        self.assertNotIn('http', item['message_template'][1])
        self.assertIn('review_link:https://mail.tsim.in/review.html', item['message_content'][1])
        self.assertEqual(len(summary[0]), 1)
        self.assertEqual(len(exceptions), 1)

    def test_non_app_user_gets_install_invitation_only(self):
        inserts, _, _, _ = self.run_process([self.row(app=False)])
        self.assertEqual(inserts[0]['notification_type'], ['email'])
        self.assertIn('Download', inserts[0]['message_template'][0])
        self.assertEqual(feedback.NOTIFICATION_TYPE, ['email'])
        self.assertEqual(feedback.STATUS_EMAIL, ['email_pending'])

    def test_duplicates_timing_and_exclusions(self):
        rows = [self.row(), self.row(email='CUSTOMER@example.com'),
                self.row(email='excluded@example.com'),
                self.row(email='buyer@marketplace.amazon.de'),
                self.row(email='early@example.com', start=date.today())]
        inserts, summary, _, _ = self.run_process(rows)
        self.assertEqual(len(inserts), 1)
        self.assertEqual([len(group) for group in summary], [1, 2, 1, 1])

    def test_failed_insert_does_not_suppress_retry(self):
        inserts, summary, exceptions, _ = self.run_process(
            [self.row(), self.row()], insert_results=[None, 1])
        self.assertEqual(len(inserts), 2)
        self.assertEqual(len(exceptions), 1)
        self.assertEqual([len(group) for group in summary], [1, 1, 0, 0])

    def test_dry_run_does_not_insert_or_write_exceptions(self):
        inserts, summary, exceptions, logs = self.run_process([self.row()], dry_run=True)
        self.assertEqual(inserts, [])
        self.assertEqual(exceptions, [])
        self.assertEqual(len(summary[0]), 1)
        self.assertTrue(any('DRY RUN templates' in message for message in logs))
        # The real summary sender exits before invoking sendmail in dry-run mode.
        with patch.multiple(feedback, DRY_RUN=True, write_log=Mock()), patch.object(feedback.subprocess, 'Popen') as sendmail:
            feedback.send_summary_email(*summary)
            sendmail.assert_not_called()

    def test_query_failure_does_not_report_success(self):
        connection = Mock()
        connection.cursor.return_value.execute.side_effect = RuntimeError('insert failed')
        driver = types.SimpleNamespace(connect=Mock(return_value=connection))
        with patch.multiple(feedback, psycopg2=driver, write_log=Mock()):
            self.assertIsNone(feedback.queryDB(feedback.NOTIFICATION_INSERT_QUERY, {}))
        connection.commit.assert_not_called()
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
