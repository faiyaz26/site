import logging
from datetime import datetime

import pytz
from django import db
from django.utils import timezone

from judge import event_poster as event
from judge.caching import finished_submission
from judge.models import Submission, SubmissionTestCase, Judge, Problem, Language
from .daemon import AMQPResponseDaemon

import time

logger = logging.getLogger('judge.handler')

UPDATE_RATE_LIMIT = 5
UPDATE_RATE_TIME = 0.5

def _ensure_connection():
    try:
        db.connection.cursor().execute('SELECT 1').fetchall()
    except Exception:
        db.connection.close()


class AMQPJudgeResponseDaemon(AMQPResponseDaemon):

    def __init__(self):
        super(AMQPJudgeResponseDaemon, self).__init__()
        # each value is (updates, last reset)
        self.update_counter = {}
    
    def on_acknowledged(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_acknowledged(packet)
        _ensure_connection()

        try:
            submission = Submission.objects.get(id=packet['id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['id'])
            return
        submission.status = 'P'

        try:
            submission.judged_on = Judge.objects.get(name=packet['judge'])
        except Judge.DoesNotExist:
            # Just in case. Is not necessary feature and is not worth the crash.
            pass

        submission.save()
        event.post('sub_%d' % submission.id, {'type': 'processing'})
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'processing', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_grading_begin(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_grading_begin(packet)
        try:
            submission = Submission.objects.get(id=packet['id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['id'])
            return
        submission.status = 'G'
        submission.current_testcase = 1
        submission.batch = False
        submission.save()
        SubmissionTestCase.objects.filter(submission_id=submission.id).delete()
        event.post('sub_%d' % submission.id, {'type': 'grading-begin'})
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'grading-begin', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_aborted(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_aborted(packet)
        try:
            submission = Submission.objects.get(id=packet['id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['id'])
            return
        submission.status = submission.result = 'AB'
        submission.is_being_rejudged = False
        submission.save()
        if not submission.problem.is_public:
            return
        event.post('sub_%d' % submission.id, {
            'type': 'aborted-submission'
        })
        self.update_counter.pop(submission.id, None)
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'terminated', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_internal_error(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_internal_error(packet)
        try:
            submission = Submission.objects.get(id=packet['id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['id'])
            return
        submission.error = packet['message']
        submission.status = submission.result = 'IE'
        submission.is_being_rejudged = False
        submission.save()
        event.post('sub_%d' % submission.id, {
            'type': 'internal-error'
        })
        self.update_counter.pop(submission.id, None)
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'internal-error', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_compile_error(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_compile_error(packet)
        try:
            submission = Submission.objects.get(id=packet['id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['id'])
            return
        submission.status = submission.result = 'CE'
        submission.error = packet['log']
        submission.is_being_rejudged = False
        submission.save()
        event.post('sub_%d' % submission.id, {
            'type': 'compile-error',
            'log': packet['log']
        })
        self.update_counter.pop(submission.id, None)
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'compile-error', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_compile_message(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_compile_message(packet)
        try:
            submission = Submission.objects.get(id=packet['id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['id'])
            return
        submission.error = packet['log']
        submission.save()
        event.post('sub_%d' % submission.id, {
            'type': 'compile-message'
        })

    def on_test_case(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_test_case(packet)
        try:
            submission = Submission.objects.get(id=packet['id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['id'])
            return
        test_case = SubmissionTestCase(submission=submission, case=packet['position'])
        status = packet['status']
        if status & 4:
            test_case.status = 'TLE'
        elif status & 8:
            test_case.status = 'MLE'
        elif status & 64:
            test_case.status = 'OLE'
        elif status & 2:
            test_case.status = 'RTE'
        elif status & 16:
            test_case.status = 'IR'
        elif status & 1:
            test_case.status = 'WA'
        elif status & 32:
            test_case.status = 'SC'
        else:
            test_case.status = 'AC'
        test_case.time = packet['time']
        test_case.memory = packet['memory']
        test_case.points = packet['points']
        test_case.total = packet['total-points']
        test_case.batch = packet['batch']
        test_case.feedback = packet.get('feedback', None) or ''
        test_case.output = packet['output']
        submission.current_testcase = packet['position'] + 1
        submission.save()
        test_case.save()
        
        do_post = True
        
        if submission.id in self.update_counter:
            cnt, reset = self.update_counter[submission.id]
            cnt += 1
            if time.time() - reset > UPDATE_RATE_TIME:
                del self.update_counter[submission.id]
            else:
                self.update_counter[submission.id] = (cnt, reset)
                if cnt > UPDATE_RATE_LIMIT:
                    do_post = False
        if submission.id not in self.update_counter:
            self.update_counter[submission.id] = (1, time.time())
        
        if do_post:
            event.post('sub_%d' % submission.id, {
                'type': 'test-case',
                'id': packet['position'],
                'status': test_case.status,
                'time': "%.3f" % round(float(packet['time']), 3),
                'memory': packet['memory'],
                'points': float(test_case.points),
                'total': float(test_case.total),
                'output': packet['output']
            })
        if not submission.problem.is_public or not do_post:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'test-case', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_grading_end(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_grading_end(packet)
        try:
            submission = Submission.objects.get(id=packet['id'])
        except Submission.DoesNotExist:
            logger.warning('Unknown submission: %d', packet['id'])
            return

        time = 0
        memory = 0
        points = 0.0
        total = 0
        status = 0
        status_codes = ['SC', 'AC', 'WA', 'MLE', 'TLE', 'IR', 'RTE', 'OLE']
        batches = {}  # batch number: (points, total)

        for case in SubmissionTestCase.objects.filter(submission=submission):
            time += case.time
            if not case.batch:
                points += case.points
                total += case.total
            else:
                if case.batch in batches:
                    batches[case.batch][0] = min(batches[case.batch][0], case.points)
                    batches[case.batch][1] = max(batches[case.batch][1], case.total)
                else:
                    batches[case.batch] = [case.points, case.total]
            memory = max(memory, case.memory)
            i = status_codes.index(case.status)
            if i > status:
                status = i

        for i in batches:
            points += batches[i][0]
            total += batches[i][1]

        points = round(points, 1)
        total = round(total, 1)
        submission.case_points = points
        submission.case_total = total

        sub_points = round(points / total * submission.problem.points if total > 0 else 0, 1)
        if not submission.problem.partial and sub_points != submission.problem.points:
            sub_points = 0

        submission.status = 'D'
        submission.time = time
        submission.memory = memory
        submission.points = sub_points
        submission.result = status_codes[status]
        submission.is_being_rejudged = False
        submission.save()

        submission.user.calculate_points()

        if hasattr(submission, 'contest'):
            contest = submission.contest
            contest.points = round(points / total * contest.problem.points if total > 0 else 0, 1)
            if not contest.problem.partial and contest.points != contest.problem.points:
                contest.points = 0
            contest.save()
            submission.contest.participation.recalculate_score()
            submission.contest.participation.update_cumtime()

        finished_submission(submission)
        self.update_counter.pop(submission.id, None)

        event.post('sub_%d' % submission.id, {
            'type': 'grading-end',
            'time': time,
            'memory': memory,
            'points': float(points),
            'total': float(submission.problem.points),
            'result': submission.result
        })
        if hasattr(submission, 'contest'):
            participation = submission.contest.participation
            event.post('contest_%d' % participation.contest_id, {'type': 'update'})
        if not submission.problem.is_public:
            return
        event.post('submissions', {'type': 'update-submission', 'id': submission.id,
                                   'state': 'grading-end', 'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})
        event.post('submissions', {'type': 'done-submission', 'id': submission.id,
                                   'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id})

    def on_executor_update(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_executor_update(packet)
        judge = Judge.objects.get(name=packet['judge'])
        judge.runtimes = Language.objects.filter(key__in=packet['executors'])
        judge.save()

    def on_problem_update(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_problem_update(packet)
        judge = Judge.objects.get(name=packet['judge'])
        judge.problems = Problem.objects.filter(code__in=packet['problems'])
        judge.save()

    def on_ping(self, packet):
        super(AMQPJudgeResponseDaemon, self).on_ping(packet)
        Judge.objects.filter(name=packet['judge']).update(
                ping=packet.get('latency'), load=packet.get('load'), last_ping=timezone.now(),
                start_time=timezone.make_aware(datetime.utcfromtimestamp(packet['start']), pytz.utc)
                if 'start' in packet else timezone.now()
        )
