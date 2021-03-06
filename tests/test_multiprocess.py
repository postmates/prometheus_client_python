from __future__ import unicode_literals

from fcntl import LOCK_EX, LOCK_SH
import glob
import os
import shutil
import sys
import tempfile
import time

from prometheus_client import mmap_dict, values
from prometheus_client.core import (
    CollectorRegistry, Counter, Gauge, Histogram, Sample, Summary,
)
from prometheus_client.exposition import generate_latest
import prometheus_client.multiprocess
from prometheus_client.multiprocess import (
    advisory_lock, archive_metrics, InMemoryCollector, mark_process_dead,
    merge, MultiProcessCollector
)
from prometheus_client.values import MultiProcessValue, MutexValue

if sys.version_info < (2, 7):
    # We need the skip decorators from unittest2 on Python 2.6.
    import unittest2 as unittest
else:
    import unittest


class TestMultiProcess(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        os.environ['prometheus_multiproc_dir'] = self.tempdir
        values.ValueClass = MultiProcessValue(lambda: 123)
        self.registry = CollectorRegistry()
        self.collector = MultiProcessCollector(self.registry, self.tempdir)

    @property
    def _value_class(self):
        return

    def tearDown(self):
        del os.environ['prometheus_multiproc_dir']
        shutil.rmtree(self.tempdir)
        values.ValueClass = MutexValue

    def test_counter_adds(self):
        c1 = Counter('c', 'help', registry=None)
        values.ValueClass = MultiProcessValue(lambda: 456)
        c2 = Counter('c', 'help', registry=None)
        self.assertEqual(0, self.registry.get_sample_value('c_total'))
        c1.inc(1)
        c2.inc(2)
        self.assertEqual(3, self.registry.get_sample_value('c_total'))

    def test_summary_adds(self):
        s1 = Summary('s', 'help', registry=None)
        values.ValueClass = MultiProcessValue(lambda: 456)
        s2 = Summary('s', 'help', registry=None)
        self.assertEqual(0, self.registry.get_sample_value('s_count'))
        self.assertEqual(0, self.registry.get_sample_value('s_sum'))
        s1.observe(1)
        s2.observe(2)
        self.assertEqual(2, self.registry.get_sample_value('s_count'))
        self.assertEqual(3, self.registry.get_sample_value('s_sum'))

    def test_histogram_adds(self):
        h1 = Histogram('h', 'help', registry=None)
        values.ValueClass = MultiProcessValue(lambda: 456)
        h2 = Histogram('h', 'help', registry=None)
        self.assertEqual(0, self.registry.get_sample_value('h_count'))
        self.assertEqual(0, self.registry.get_sample_value('h_sum'))
        self.assertEqual(0, self.registry.get_sample_value('h_bucket', {'le': '5.0'}))
        h1.observe(1)
        h2.observe(2)
        self.assertEqual(2, self.registry.get_sample_value('h_count'))
        self.assertEqual(3, self.registry.get_sample_value('h_sum'))
        self.assertEqual(2, self.registry.get_sample_value('h_bucket', {'le': '5.0'}))

    def test_gauge_all(self):
        values.ValueClass = MultiProcessValue(lambda: 123)
        g1 = Gauge('g', 'help', registry=None, multiprocess_mode='all')
        values.ValueClass = MultiProcessValue(lambda: 456)
        g2 = Gauge('g', 'help', registry=None, multiprocess_mode='all')
        self.assertEqual(0, self.registry.get_sample_value('g', {'pid': '123'}))
        self.assertEqual(0, self.registry.get_sample_value('g', {'pid': '456'}))
        g1.set(1)
        g2.set(2)
        archive_metrics()
        mark_process_dead(123, os.environ['prometheus_multiproc_dir'])
        self.assertEqual(1, self.registry.get_sample_value('g', {'pid': '123'}))
        self.assertEqual(2, self.registry.get_sample_value('g', {'pid': '456'}))

    def test_gauge_liveall(self):
        g1 = Gauge('g', 'help', registry=None, multiprocess_mode='liveall')
        self.assertEqual(0, self.registry.get_sample_value('g', {'pid': '123'}))
        g1.set(1)
        values.ValueClass = MultiProcessValue(lambda: 456)
        g2 = Gauge('g', 'help', registry=None, multiprocess_mode='liveall')
        self.assertEqual(0, self.registry.get_sample_value('g', {'pid': '456'}))
        g2.set(2)
        self.assertEqual(1, self.registry.get_sample_value('g', {'pid': '123'}))
        self.assertEqual(2, self.registry.get_sample_value('g', {'pid': '456'}))
        mark_process_dead(123, os.environ['prometheus_multiproc_dir'])

        self.assertEqual(None, self.registry.get_sample_value('g', {'pid': '123'}))
        self.assertEqual(2, self.registry.get_sample_value('g', {'pid': '456'}))

    def test_gauge_latest(self):
        self.assertEqual(None, self.registry.get_sample_value('g'))
        g1 = Gauge('g', 'G', registry=None, multiprocess_mode=Gauge.LATEST)
        g1.set(0)
        self.assertEqual(0, self.registry.get_sample_value('g'))
        g1.set(123)
        self.assertEqual(123, self.registry.get_sample_value('g'))

        t0 = time.time()
        g1.set(1, timestamp=t0)
        self.assertEqual(1, self.registry.get_sample_value('g'))
        archive_metrics()
        self.assertEqual(1, self.registry.get_sample_value('g'))
        values.ValueClass = MultiProcessValue(lambda: '456789')
        g2 = Gauge('g', 'G', registry=None, multiprocess_mode=Gauge.LATEST)
        t1 = t0 - time.time()
        g2.set(2, timestamp=t1)
        self.assertEqual(1, self.registry.get_sample_value('g'))
        archive_metrics()
        self.assertEqual(1, self.registry.get_sample_value('g'))

    def test_gauge_min(self):
        g1 = Gauge('g', 'help', registry=None, multiprocess_mode='min')
        values.ValueClass = MultiProcessValue(lambda: 456)
        g2 = Gauge('g', 'help', registry=None, multiprocess_mode='min')
        self.assertEqual(0, self.registry.get_sample_value('g'))
        g1.set(1)
        g2.set(2)
        self.assertEqual(1, self.registry.get_sample_value('g'))

    def test_gauge_max(self):
        g1 = Gauge('g', 'help', registry=None, multiprocess_mode='max')
        values.ValueClass = MultiProcessValue(lambda: 456)
        g2 = Gauge('g', 'help', registry=None, multiprocess_mode='max')
        self.assertEqual(0, self.registry.get_sample_value('g'))
        g1.set(1)
        g2.set(2)
        self.assertEqual(2, self.registry.get_sample_value('g'))

    def test_gauge_livesum(self):
        g1 = Gauge('g', 'help', registry=None, multiprocess_mode='livesum')
        values.ValueClass = MultiProcessValue(lambda: 456)
        g2 = Gauge('g', 'help', registry=None, multiprocess_mode='livesum')
        self.assertEqual(0, self.registry.get_sample_value('g'))
        g1.set(1)
        g2.set(2)
        self.assertEqual(3, self.registry.get_sample_value('g'))
        mark_process_dead(123, os.environ['prometheus_multiproc_dir'])
        self.assertEqual(2, self.registry.get_sample_value('g'))

    def test_namespace_subsystem(self):
        c1 = Counter('c', 'help', registry=None, namespace='ns', subsystem='ss')
        c1.inc(1)
        self.assertEqual(1, self.registry.get_sample_value('ns_ss_c_total'))

    def test_counter_across_forks(self):
        pid = 0
        values.ValueClass = MultiProcessValue(lambda: pid)
        c1 = Counter('c', 'help', registry=None)
        self.assertEqual(0, self.registry.get_sample_value('c_total'))
        c1.inc(1)
        c1.inc(1)
        pid = 1
        c1.inc(1)
        self.assertEqual(3, self.registry.get_sample_value('c_total'))
        self.assertEqual(1, c1._value.get())

    def test_initialization_detects_pid_change(self):
        pid = 0
        values.ValueClass = MultiProcessValue(lambda: pid)

        # can not inspect the files cache directly, as it's a closure, so we
        # check for the actual files themselves
        def files():
            fs = os.listdir(os.environ['prometheus_multiproc_dir'])
            fs.sort()
            return fs

        c1 = Counter('c1', 'c1', registry=None)
        self.assertEqual(files(), ['counter_0.db'])
        c2 = Counter('c2', 'c2', registry=None)
        self.assertEqual(files(), ['counter_0.db'])
        pid = 1
        c3 = Counter('c3', 'c3', registry=None)
        self.assertEqual(files(), ['counter_0.db', 'counter_1.db'])

    @unittest.skipIf(sys.version_info < (2, 7), "Test requires Python 2.7+.")
    def test_collect(self):
        pid = 0
        values.ValueClass = MultiProcessValue(lambda: pid)
        labels = dict((i, i) for i in 'abcd')

        def add_label(key, value):
            l = labels.copy()
            l[key] = value
            return l

        c = Counter('c', 'help', labelnames=labels.keys(), registry=None)
        g = Gauge('g', 'help', labelnames=labels.keys(), registry=None,
                  multiprocess_mode='all')
        h = Histogram('h', 'help', labelnames=labels.keys(), registry=None)

        c.labels(**labels).inc(1)
        g.labels(**labels).set(1)
        h.labels(**labels).observe(1)

        pid = 1

        c.labels(**labels).inc(1)
        g.labels(**labels).set(1)
        h.labels(**labels).observe(5)

        metrics = dict((m.name, m) for m in self.collector.collect())

        self.assertEqual(
            metrics['c'].samples, [Sample('c_total', labels, 2.0)]
        )
        metrics['g'].samples.sort(key=lambda x: x[1]['pid'])
        self.assertEqual(metrics['g'].samples, [
            Sample('g', add_label('pid', '0'), 1.0),
            Sample('g', add_label('pid', '1'), 1.0),
        ])

        metrics['h'].samples.sort(
            key=lambda x: (x[0], float(x[1].get('le', 0)))
        )
        expected_histogram = [
            Sample('h_bucket', add_label('le', '0.005'), 0.0),
            Sample('h_bucket', add_label('le', '0.01'), 0.0),
            Sample('h_bucket', add_label('le', '0.025'), 0.0),
            Sample('h_bucket', add_label('le', '0.05'), 0.0),
            Sample('h_bucket', add_label('le', '0.075'), 0.0),
            Sample('h_bucket', add_label('le', '0.1'), 0.0),
            Sample('h_bucket', add_label('le', '0.25'), 0.0),
            Sample('h_bucket', add_label('le', '0.5'), 0.0),
            Sample('h_bucket', add_label('le', '0.75'), 0.0),
            Sample('h_bucket', add_label('le', '1.0'), 1.0),
            Sample('h_bucket', add_label('le', '2.5'), 1.0),
            Sample('h_bucket', add_label('le', '5.0'), 2.0),
            Sample('h_bucket', add_label('le', '7.5'), 2.0),
            Sample('h_bucket', add_label('le', '10.0'), 2.0),
            Sample('h_bucket', add_label('le', '+Inf'), 2.0),
            Sample('h_count', labels, 2.0),
            Sample('h_sum', labels, 6.0),
        ]

        self.assertEqual(metrics['h'].samples, expected_histogram)

    @unittest.skipIf(sys.version_info < (2, 7), "Test requires Python 2.7+.")
    def test_merge_no_accumulate(self):
        pid = 0
        values.ValueClass = MultiProcessValue(lambda: pid)
        labels = dict((i, i) for i in 'abcd')

        def add_label(key, value):
            l = labels.copy()
            l[key] = value
            return l

        h = Histogram('h', 'help', labelnames=labels.keys(), registry=None)
        h.labels(**labels).observe(1)
        pid = 1
        h.labels(**labels).observe(5)

        path = os.path.join(os.environ['prometheus_multiproc_dir'], '*.db')
        files = glob.glob(path)
        metrics = dict(
            (m.name, m) for m in merge(files, accumulate=False)
        )

        metrics['h'].samples.sort(
            key=lambda x: (x[0], float(x[1].get('le', 0)))
        )
        expected_histogram = [
            Sample('h_bucket', add_label('le', '0.005'), 0.0),
            Sample('h_bucket', add_label('le', '0.01'), 0.0),
            Sample('h_bucket', add_label('le', '0.025'), 0.0),
            Sample('h_bucket', add_label('le', '0.05'), 0.0),
            Sample('h_bucket', add_label('le', '0.075'), 0.0),
            Sample('h_bucket', add_label('le', '0.1'), 0.0),
            Sample('h_bucket', add_label('le', '0.25'), 0.0),
            Sample('h_bucket', add_label('le', '0.5'), 0.0),
            Sample('h_bucket', add_label('le', '0.75'), 0.0),
            Sample('h_bucket', add_label('le', '1.0'), 1.0),
            Sample('h_bucket', add_label('le', '2.5'), 0.0),
            Sample('h_bucket', add_label('le', '5.0'), 1.0),
            Sample('h_bucket', add_label('le', '7.5'), 0.0),
            Sample('h_bucket', add_label('le', '10.0'), 0.0),
            Sample('h_bucket', add_label('le', '+Inf'), 0.0),
            Sample('h_sum', labels, 6.0),
        ]

        self.assertEqual(metrics['h'].samples, expected_histogram)


    def test_missing_gauge_file_during_merge(self):
        # These files don't exist, just like if mark_process_dead(9999999) had been
        # called during self.collector.collect(), after the glob found it
        # but before the merge actually happened.
        # This should not raise and return no metrics
        self.assertFalse(merge([
            os.path.join(self.tempdir, 'gauge_liveall_9999999.db'),
            os.path.join(self.tempdir, 'gauge_livesum_9999999.db'),
        ]))


class TestMmapedDict(unittest.TestCase):
    def setUp(self):
        fd, self.tempfile = tempfile.mkstemp()
        os.close(fd)
        self.d = mmap_dict.MmapedDict(self.tempfile)

    def test_timestamp(self):
        t0 = time.time()
        self.d.write_value("foo", 3.0, timestamp=t0)
        v, t = self.d.read_value_timestamp("foo")
        self.assertTrue((v - 3.0) ** 2 < 0.001)
        self.assertTrue((t0 - t) ** 2 < 0.001)

    def test_process_restart(self):
        self.d.write_value('abc', 123.0)
        self.d.close()
        self.d = mmap_dict.MmapedDict(self.tempfile)
        self.assertEqual(123, self.d.read_value('abc'))
        self.assertEqual([('abc', 123.0, None)], list(self.d.read_all_values()))

    def test_expansion(self):
        key = 'a' * mmap_dict._INITIAL_MMAP_SIZE
        self.d.write_value(key, 123.0)
        self.assertEqual([(key, 123.0, None)], list(self.d.read_all_values()))

    def test_multi_expansion(self):
        key = 'a' * mmap_dict._INITIAL_MMAP_SIZE * 4
        self.d.write_value('abc', 42.0)
        self.d.write_value(key, 123.0)
        self.d.write_value('def', 17.0)
        self.assertEqual(
            [('abc', 42.0, None), (key, 123.0, None), ('def', 17.0, None)],
            list(self.d.read_all_values()))

    def test_corruption_detected(self):
        self.d.write_value('abc', 42.0)
        # corrupt the written data
        self.d._m[8:16] = b'somejunk'
        with self.assertRaises(RuntimeError):
            list(self.d.read_all_values())

    def tearDown(self):
        os.unlink(self.tempfile)


class TestUnsetEnv(unittest.TestCase):
    def setUp(self):
        self.registry = CollectorRegistry()
        fp, self.tmpfl = tempfile.mkstemp()
        os.close(fp)

    def test_unset_syncdir_env(self):
        self.assertRaises(
            ValueError, MultiProcessCollector, self.registry)

    def test_file_syncpath(self):
        registry = CollectorRegistry()
        self.assertRaises(
            ValueError, MultiProcessCollector, registry, self.tmpfl)

    def tearDown(self):
        os.remove(self.tmpfl)


class TestAdvisoryLock(unittest.TestCase):
    """
    These tests use lock aqusition as a proxy for cleanup/collect operations,
    the former using exclusive locks, the latter shared locks
    """
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        os.environ['prometheus_multiproc_dir'] = self.tempdir
        values.ValueClass = MultiProcessValue(lambda: 123)
        self.registry = CollectorRegistry()
        self.collector = MultiProcessCollector(self.registry, self.tempdir)

    def test_cleanup_waits_for_collectors(self):
        # IOError in python2, OSError in python3
        with self.assertRaises(EnvironmentError):
            with advisory_lock(LOCK_SH):
                archive_metrics(blocking=False)

    def test_collect_doesnt_block_other_collects(self):
        values.ValueClass = MultiProcessValue(lambda: 0)
        labels = dict((i, i) for i in 'abcd')
        c = Counter('c', 'help', labelnames=labels.keys(), registry=None)
        c.labels(**labels).inc(1)

        with advisory_lock(LOCK_SH):
            metrics = dict((m.name, m) for m in self.collector.collect(blocking=False))
            self.assertEqual(
                metrics['c'].samples, [Sample('c_total', labels, 1.0)]
            )

    def test_collect_waits_for_cleanup(self):
        values.ValueClass = MultiProcessValue(lambda: 0)
        labels = dict((i, i) for i in 'abcd')
        c = Counter('c', 'help', labelnames=labels.keys(), registry=None)
        c.labels(**labels).inc(1)
        with self.assertRaises(EnvironmentError):
            with advisory_lock(LOCK_EX):
                self.collector.collect(blocking=False)

    def test_exceptions_release_lock(self):
        with self.assertRaises(ValueError):
            with advisory_lock(LOCK_EX):
                raise ValueError
        # Do an operation which requires acquiring the lock
        archive_metrics(blocking=False)

    def tearDown(self):
        del os.environ['prometheus_multiproc_dir']
        shutil.rmtree(self.tempdir)
        values.ValueClass = MutexValue


class TestInMemoryCollector(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        os.environ['prometheus_multiproc_dir'] = self.tempdir
        values.ValueClass = MultiProcessValue(lambda: 123)
        self.registry = CollectorRegistry()
        self.collector = InMemoryCollector(self.registry)

    def tearDown(self):
        del os.environ['prometheus_multiproc_dir']
        shutil.rmtree(self.tempdir)
        values.ValueClass = MutexValue
        prometheus_client.multiprocess._metrics_cache = prometheus_client.multiprocess.MetricsCache()

    def test_serves_empty_metrics_if_no_metrics_written(self):
        self.assertEqual(self.collector.collect(), [])

    def test_serves_empty_metrics_if_not_processed(self):
        c1 = Counter('c', 'help', registry=None)
        # The cleanup/archiver task hasn't run yet, no metrics
        self.assertEqual(None, self.registry.get_sample_value('c_total'))
        c1.inc(1)
        # Still no metrics
        self.assertEqual(self.collector.collect(), [])

    def test_serves_metrics(self):
        labels = dict((i, i) for i in 'abcd')
        c = Counter('c', 'help', labelnames=labels.keys(), registry=None)
        c.labels(**labels).inc(1)
        self.assertEqual(None, self.registry.get_sample_value('c_total', labels))
        archive_metrics()
        self.assertEqual(self.collector.collect()[0].samples, [Sample('c_total', labels, 1.0)])

    def test_displays_archive_stats(self):
        output = generate_latest(self.registry)
        self.assertIn("archive_duration_seconds", output)

    def test_aggregates_live_and_archived_metrics(self):
        pid = 456 
        values.ValueClass = MultiProcessValue(lambda: pid)

        def files():
            fs = os.listdir(os.environ['prometheus_multiproc_dir'])
            fs.sort()
            return fs
        c1 = Counter('c1', 'c1', registry=None)
        c1.inc(1)
        self.assertIn('counter_456.db', files())

        archive_metrics()
        self.assertNotIn('counter_456.db', files())
        self.assertEqual(1, self.registry.get_sample_value('c1_total'))

        pid = 789
        values.ValueClass = MultiProcessValue(lambda: pid)
        c1 = Counter('c1', 'c1', registry=None)
        c1.inc(2)
        g1 = Gauge('g1', 'g1', registry=None, multiprocess_mode="liveall")
        g1.set(5)
        self.assertIn('counter_789.db', files())
        # Pretend that pid 789 is live
        archive_metrics(aggregate_only=True)

        # The live counter should be merged with the archived counter, and the
        # liveall gauge should be included
        self.assertIn('counter_789.db', files())
        self.assertIn('gauge_liveall_789.db', files())
        self.assertEqual(3, self.registry.get_sample_value('c1_total'))
        self.assertEqual(5, self.registry.get_sample_value('g1', labels={u'pid': u'789'}))
        # Now pid 789 is dead
        archive_metrics()

        # The formerly live counter's value should be archived, and the
        # liveall gauge should be removed completely
        self.assertNotIn('counter_789.db', files())
        self.assertNotIn('gauge_liveall_789.db', files())
        self.assertEqual(3, self.registry.get_sample_value('c1_total'))
        self.assertEqual(None, self.registry.get_sample_value('g1', labels={u'pid': u'789'}))
