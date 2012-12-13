
# A test run filter ...

# The data that makes up a filter are:
#
# * A non-empty set of bundle streams
# * A possibly empty set of (attribute-name, attribute-value) pairs
# * A possibly empty list of tests, each of which has a possibly empty list of
#   test cases
# * An optional build number attribute name

# We define several representations for this data:
#
# * One is the TestRunFilter and related tables (the "model represenation").
#   These have some representation specific metadata that does not relate to
#   the test runs the filter selects: names, owner, the "public" flag.

# * One is the natural Python data structure for the data (the "in-memory
#   representation"), i.e.
#     {
#         bundle_streams: [<BundleStream objects>],
#         attributes: [(attr-name, attr-value)],
#         tests: [{"test": <Test instance>, "test_cases":[<TestCase instances>]}],
#         build_number_attribute: attr-name-or-None,
#         uploaded_by: <User instance-or-None>,
#     }
#   This is the representation that is used to evaluate a filter (so that
#   previewing new filters can be done without having to create a
#   TestRunFilter instance that we carefully don't save to the database --
#   which doesn't work very well anyway with all the ManyToMany relations
#   involved)

# * One is this datastructure with model instances replaced by identifying
#   strings (the "serializable representation"):
#     {
#         bundle_streams: [pathnames],
#         attributes: [(attr-name, attr-value)],
#         tests: [{"test": test_id, "test_cases":[test_case_id]}],
#         build_number_attribute: attr-name-or-None,
#         uploaded_by: username-or-None,
#     }
#   This is useful because it can be serialized as JSON.  XXX It also doesn't
#   exist yet!

# * The final one is the TRFForm object defined in
#   dashboard_app.views.filters.forms (the "form representation")
#   (pedantically, the rendered form of this is yet another
#   representation...).  This representation is the only one other than the
#   model objects to include the name/owner/public metadata.

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.sql.aggregates import Aggregate as SQLAggregate

from dashboard_app.models import (
    BundleStream,
    NamedAttribute,
    TestResult,
    TestRun,
    )


class FilterMatch(object):
    """A non-database object that represents the way a filter matches a test_run.

    Returned by TestRunFilter.matches_against_bundle and
    TestRunFilter.get_test_runs.
    """

    filter = None # The model representation of the filter (this is only set
                  # by matches_against_bundle)
    filter_data = None # The in-memory representation of the filter.
    tag = None # either a date (bundle__uploaded_on) or a build number
    test_runs = None
    specific_results = None # Will stay none unless filter specifies a test case
    pass_count = None # Only filled out for filters that dont specify a test

    def _format_test_result(self, result):
        prefix = result.test_case.test.test_id + ':' + result.test_case.test_case_id + ' '
        if result.test_case.units:
            return prefix + '%s%s' % (result.measurement, result.units)
        else:
            return prefix + result.RESULT_MAP[result.result]

    def _format_test_run(self, tr):
        return "%s %s pass / %s total" % (
            tr.test.test_id,
            tr.denormalization.count_pass,
            tr.denormalization.count_all())

    def _format_many_test_runs(self):
        return "%s pass / %s total" % (self.pass_count, self.result_count)

    def format_for_mail(self):
        r = [' ~%s/%s ' % (self.filter.owner.username, self.filter.name)]
        if not self.filter_data['tests']:
            r.append(self._format_many_test_runs())
        else:
            for test in self.filter_data['tests']:
                if not test['test_cases']:
                    for tr in self.test_runs:
                        if tr.test == test.test:
                            r.append('\n    ')
                            r.append(self._format_test_run(tr))
                for test_case in test['test_cases']:
                    for result in self.specific_results:
                        if result.test_case.id == test_case.id:
                            r.append('\n    ')
                            r.append(self._format_test_result(result))
        r.append('\n')
        return ''.join(r)


class MatchMakingQuerySet(object):
    """Wrap a QuerySet and construct FilterMatchs from what the wrapped query
    set returns.

    Just enough of the QuerySet API to work with DataTable (i.e. pretend
    ordering and real slicing)."""

    model = TestRun

    def __init__(self, queryset, filter_data, prefetch_related):
        self.queryset = queryset
        self.filter_data = filter_data
        self.prefetch_related = prefetch_related
        if filter_data['build_number_attribute']:
            self.key = 'build_number'
            self.key_name = 'Build'
        else:
            self.key = 'bundle__uploaded_on'
            self.key_name = 'Uploaded On'

    def _makeMatches(self, data):
        test_run_ids = set()
        for datum in data:
            test_run_ids.update(datum['id__arrayagg'])
        r = []
        trs = TestRun.objects.filter(id__in=test_run_ids).select_related(
            'denormalization', 'bundle', 'bundle__bundle_stream', 'test').prefetch_related(
            *self.prefetch_related)
        trs_by_id = {}
        for tr in trs:
            trs_by_id[tr.id] = tr
        case_ids = set()
        for t in self.filter_data['tests']:
            for case in t['test_cases']:
                case_ids.add(case.id)
        if case_ids:
            result_ids_by_tr_id = {}
            results_by_tr_id = {}
            values = TestResult.objects.filter(
                test_case__id__in=case_ids,
                test_run__id__in=test_run_ids).values_list(
                'test_run__id', 'id')
            result_ids = set()
            for v in values:
                result_ids_by_tr_id.setdefault(v[0], []).append(v[1])
                result_ids.add(v[1])

            results_by_id = {}
            for result in TestResult.objects.filter(
                id__in=list(result_ids)).select_related(
                'test', 'test_case', 'test_run__bundle__bundle_stream'):
                results_by_id[result.id] = result

            for tr_id, result_ids in result_ids_by_tr_id.items():
                rs = results_by_tr_id[tr_id] = []
                for result_id in result_ids:
                    rs.append(results_by_id[result_id])
        for datum in data:
            trs = []
            for id in set(datum['id__arrayagg']):
                trs.append(trs_by_id[id])
            match = FilterMatch()
            match.test_runs = trs
            match.filter_data = self.filter_data
            match.tag = datum[self.key]
            if case_ids:
                match.specific_results = []
                for id in set(datum['id__arrayagg']):
                    match.specific_results.extend(results_by_tr_id.get(id, []))
            else:
                match.pass_count = sum(tr.denormalization.count_pass for tr in trs)
                match.result_count = sum(tr.denormalization.count_all() for tr in trs)
            r.append(match)
        return iter(r)

    def _wrap(self, queryset, **kw):
        return self.__class__(queryset, self.filter_data, self.prefetch_related, **kw)

    def order_by(self, *args):
        # the generic tables code calls this even when it shouldn't...
        return self

    def count(self):
        return self.queryset.count()

    def __getitem__(self, item):
        return self._wrap(self.queryset[item])

    def __iter__(self):
        data = list(self.queryset)
        return self._makeMatches(data)


class SQLArrayAgg(SQLAggregate):
    sql_function = 'array_agg'


class ArrayAgg(models.Aggregate):
    name = 'ArrayAgg'
    def add_to_query(self, query, alias, col, source, is_summary):
        aggregate = SQLArrayAgg(
            col, source=source, is_summary=is_summary, **self.extra)
        # For way more detail than you want about what this next line is for,
        # see
        # http://voices.canonical.com/michael.hudson/2012/09/02/using-postgres-array_agg-from-django/
        aggregate.field = models.DecimalField() # vomit
        query.aggregates[alias] = aggregate


# given filter:
# select from testrun
#  where testrun.bundle in filter.bundle_streams ^ accessible_bundles
#    and testrun has attribute with key = key1 and value = value1
#    and testrun has attribute with key = key2 and value = value2
#    and               ...
#    and testrun has attribute with key = keyN and value = valueN
#    and testrun has any of the tests/testcases requested
#    [and testrun has attribute with key = build_number_attribute]
#    [and testrun.bundle.uploaded_by = uploaded_by]
def evaluate_filter(user, filter_data, prefetch_related=[]):
    accessible_bundle_streams = BundleStream.objects.accessible_by_principal(
        user)
    bs_ids = [bs.id for bs in set(accessible_bundle_streams) & set(filter_data['bundle_streams'])]
    conditions = [models.Q(bundle__bundle_stream__id__in=bs_ids)]

    content_type_id = ContentType.objects.get_for_model(TestRun).id

    for (name, value) in filter_data['attributes']:
        # We punch through the generic relation abstraction here for 100x
        # better performance.
        conditions.append(
            models.Q(id__in=NamedAttribute.objects.filter(
                name=name, value=value, content_type_id=content_type_id
                ).values('object_id')))

    test_condition = None
    for test in filter_data['tests']:
        case_ids = set()
        for test_case in test['test_cases']:
            case_ids.add(test_case.id)
        if case_ids:
            q = models.Q(
                test__id=test['test'].id,
                test_results__test_case__id__in=case_ids)
        else:
            q = models.Q(test__id=test['test'].id)
        if test_condition:
            test_condition = test_condition | q
        else:
            test_condition = q
    if test_condition:
        conditions.append(test_condition)

    if filter_data['uploaded_by']:
        conditions.append(models.Q(bundle__uploaded_by=filter_data['uploaded_by']))

    testruns = TestRun.objects.filter(*conditions)

    if filter_data['build_number_attribute']:
        testruns = testruns.filter(
            attributes__name=filter_data['build_number_attribute']).extra(
            select={
                'build_number': 'convert_to_integer("dashboard_app_namedattribute"."value")',
                },
            where=['convert_to_integer("dashboard_app_namedattribute"."value") IS NOT NULL']).extra(
            order_by=['-build_number'],
            ).values('build_number').annotate(ArrayAgg('id'))
    else:
        testruns = testruns.order_by('-bundle__uploaded_on').values(
            'bundle__uploaded_on').annotate(ArrayAgg('id'))

    return MatchMakingQuerySet(testruns, filter_data, prefetch_related)
