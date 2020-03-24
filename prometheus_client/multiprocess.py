#!/usr/bin/python

from __future__ import unicode_literals

from collections import defaultdict
from contextlib import contextmanager
import errno
from fcntl import flock, LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN
import glob
import json
import logging
import os
import re
import shutil
import tempfile
from threading import RLock
import time

from .metrics import Counter, Gauge, Histogram
from .metrics_core import Metric
from .mmap_dict import mmap_key, MmapedDict
from .samples import Sample
from .utils import floatToGoString


PROMETHEUS_MULTIPROC_DIR = "prometheus_multiproc_dir"
_db_pattern = re.compile(r"(\w+)_(\d+)\.db")


class MetricsCache(object):
    def __init__(self):
        self.metrics = []
        self.last_scrape_time = None
        self.lock = RLock()

    def retrieve_metrics(self):
        logging.info("Retrieving metrics from: {}".format(self.last_scrape_time))
        with self.lock:
            return self.metrics

    def write_metrics(self, metrics, time_elapsed=None):
        logging.info("Time to build metrics: {}".format(time_elapsed))
        with self.lock:
            self.last_scrape_time = time.time()
            self.metrics = metrics


_metrics_cache = MetricsCache()


class InMemoryCollector(object):
    """
    A Collector which simply serves statistics collected by the archiver
    (cleanup_dead_processes())
    """

    def __init__(self, registry):
        if registry:
            registry.register(self)

    def collect(self):
        return _metrics_cache.retrieve_metrics()


class MultiProcessCollector(object):
    """Collector for files for multi-process mode."""

    def __init__(self, registry, path=None):
        if path is None:
            path = os.environ.get('prometheus_multiproc_dir')
        if not path or not os.path.isdir(path):
            raise ValueError('env prometheus_multiproc_dir is not set or not a directory')
        self._path = path
        if registry:
            registry.register(self)


    def collect(self, blocking=True):
        # blocking is used for testing purposes
        lock_type = LOCK_SH if blocking else LOCK_SH | LOCK_NB
        with advisory_lock(lock_type):
            files = glob.glob(os.path.join(self._path, '*.db'))
            return merge(files, accumulate=True)


def merge(files, accumulate=True):
    """Merge metrics from given mmap files.

    By default, histograms are accumulated, as per prometheus wire format.
    But if writing the merged data back to mmap files, use
    accumulate=False to avoid compound accumulation.
    """

    # TODO: read from 
    metrics = {}
    for f in files:
        parts = os.path.splitext(os.path.basename(f))[0].split('_')
        typ = parts[0]
        multiprocess_mode = parts[1] if typ == Gauge._type else None
        pid = parts[2] if multiprocess_mode and len(parts) > 2 else None
        try:
            d = MmapedDict(f, read_mode=True)
        except EnvironmentError:
            # The liveall and livesum gauge metrics
            # are deleted when the gunicorn/celery worker process dies
            # (mark_process_dead and, in postal-main, boot.gunicornconf.child_exit).
            # Since these are deleted without acquiring a lock, they may
            # not be present in between collecting the metrics files and
            # merging them, resulting in a FileNotFoundError/IOError.
            # However, since these gauges only care about live processes,
            # we wouldn't merge them anyway.
            # 
            # Additionally, we have a single thread which will collect
            # metrics files from dead workers, and merge them into a set of
            # archive files at regular interviews (see
            # multiprocess_exporter). This operation is protected by a
            # mutex, ensuring that no collectors are run during cleanup. We
            # must do so because other metrics are sensitive to partial
            # collection; prometheus counters cannot be decremented, as
            # prometheus assumes that, in the time since the last scrape,
            # the counter reset to 0 and incremented back up to the
            # collected value, manifesting itself as a huge rate spike
            if typ == 'gauge' and parts[1] in (Gauge.LIVESUM, Gauge.LIVEALL):
                continue
            raise

        for key, value, timestamp in d.read_all_values():
            metric_name, name, labels = json.loads(key)
            if pid:
                labels["pid"] = pid
            labels_key = tuple(sorted(labels.items()))

            metric = metrics.get(metric_name)
            if metric is None:
                metric = Metric(metric_name, 'Multiprocess metric', typ)
                metrics[metric_name] = metric
            if multiprocess_mode:
                metric._multiprocess_mode = multiprocess_mode
            metric.add_sample(name, labels_key, value, timestamp=timestamp)
        d.close()

    for metric in metrics.itervalues():
        # Handle the Gauge "latest" multiprocess mode type:
        if metric.type == Gauge._type and metric._multiprocess_mode == Gauge.LATEST:
            s = max(metric.samples, key=lambda i: i.timestamp)
            # Group samples by name, labels:
            grouped_samples = defaultdict(list)
            for s in metric.samples:
                labels = dict(s.labels)
                if "pid" in labels:
                    del labels["pid"]
                grouped_samples[s.name, tuple(sorted(labels.items()))].append(s)
            metric.samples = []
            for (name, labels), sample_group in grouped_samples.iteritems():
                s = max(sample_group, key=lambda i: i.timestamp)
                metric.samples.append(Sample(name,
                                             dict(labels),
                                             value=s.value,
                                             timestamp=s.timestamp))
            continue

        samples = defaultdict(float)
        buckets = {}
        for s in metric.samples:
            name, labels, value = s.name, s.labels, s.value
            if metric.type == Gauge._type:
                without_pid = tuple(l for l in labels if l[0] != 'pid')
                if metric._multiprocess_mode == Gauge.MIN:
                    current = samples.setdefault((name, without_pid), value)
                    if value < current:
                        samples[(s.name, without_pid)] = value
                elif metric._multiprocess_mode == Gauge.MAX:
                    current = samples.setdefault((name, without_pid), value)
                    if value > current:
                        samples[(s.name, without_pid)] = value
                elif metric._multiprocess_mode == Gauge.LIVESUM:
                    samples[(name, without_pid)] += value
                else:  # all/liveall
                    samples[(name, labels)] = value

            elif metric.type == 'histogram':
                bucket = tuple(float(l[1]) for l in labels if l[0] == 'le')
                if bucket:
                    # _bucket
                    without_le = tuple(l for l in labels if l[0] != 'le')
                    buckets.setdefault(without_le, {})
                    buckets[without_le].setdefault(bucket[0], 0.0)
                    buckets[without_le][bucket[0]] += value
                else:
                    # _sum/_count
                    samples[(s.name, labels)] += value
            else:
                # Counter and Summary.
                samples[(s.name, labels)] += value


        # Accumulate bucket values.
        if metric.type == 'histogram':
            for labels, values in buckets.items():
                acc = 0.0
                for bucket, value in sorted(values.items()):
                    sample_key = (
                        metric.name + '_bucket',
                        labels + (('le', floatToGoString(bucket)),),
                    )
                    if accumulate:
                        acc += value
                        samples[sample_key] = acc
                    else:
                        samples[sample_key] = value
                if accumulate:
                    samples[(metric.name + '_count', labels)] = acc
        # Convert to correct sample format.
        metric.samples = [Sample(name_, dict(labels), value) for (name_, labels), value in samples.items()]
    return metrics.values()


def mark_process_dead(pid, path=None):
    """Do bookkeeping for when one process dies in a multi-process setup."""
    path = _multiproc_dir() if path is None else path
    _remove_livesum_dbs(pid, path=path)


def _remove_livesum_dbs(pid, path):
    for gauge_type in [Gauge.LIVESUM, Gauge.LIVEALL]:
        _safe_remove("{}/gauge_{}_{}.db".format(path, gauge_type, pid))


def _multiproc_dir():
    return os.environ[PROMETHEUS_MULTIPROC_DIR]


def _get_archive_paths(prom_dir=None):
    prom_dir = _multiproc_dir() if prom_dir is None else prom_dir
    merged_paths = {
        (Histogram._type, None): "histogram.db",
        (Counter._type, None): "counter.db",
        (Gauge._type, Gauge.LATEST): "gauge_{}.db".format(Gauge.LATEST),
        (Gauge._type, Gauge.MAX): "gauge_{}.db".format(Gauge.MAX),
        (Gauge._type, Gauge.MIN): "gauge_{}.db".format(Gauge.MIN),
    }
    merged_paths = {
        k: os.path.join(prom_dir, f) for k, f in merged_paths.iteritems()
    }
    return merged_paths


def cleanup_process(pid, prom_dir=None):
    """Aggregate dead worker's metrics into a single archive file."""
    prom_dir = _multiproc_dir() if prom_dir is None else prom_dir

    merged_paths = _get_archive_paths(prom_dir)
    worker_paths = [
        "counter_{}.db".format(pid),
        "gauge_{}_{}.db".format(Gauge.LATEST, pid),
        "gauge_{}_{}.db".format(Gauge.MAX, pid),
        "gauge_{}_{}.db".format(Gauge.MIN, pid),
        "histogram_{}.db".format(pid),
    ]
    worker_paths = (os.path.join(prom_dir, f) for f in worker_paths)
    worker_paths = filter(os.path.exists, worker_paths)
    if worker_paths:
        all_paths = worker_paths + filter(os.path.exists, merged_paths.values())
        metrics = merge(all_paths, accumulate=False)
        _write_metrics(metrics, merged_paths)
    for worker_path in worker_paths:
        _safe_remove(worker_path)
    _remove_livesum_dbs(pid, path=prom_dir)


def _safe_remove(p):
    try:
        os.unlink(p)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def _write_metrics(metrics, metric_type_to_dst_path):
    mmaped_dicts = defaultdict(lambda: MmapedDict(tempfile.mktemp()))
    for metric in metrics:
        if metric.type not in [Histogram._type, Counter._type, Gauge._type]:
            continue
        mode = None
        if metric.type == Gauge._type:
            mode = metric._multiprocess_mode
            if mode not in [Gauge.MIN, Gauge.MAX, Gauge.LATEST]:
                continue
        sink = mmaped_dicts[metric.type, mode]

        for sample in metric.samples:
            # prometheus_client 0.4+ adds extra fields
            key = mmap_key(
                metric.name,
                sample.name,
                tuple(sample.labels),
                tuple(sample.labels.values()),
            )
            sink.write_value(key, sample.value, timestamp=sample.timestamp)
    for k, mmaped_dict in mmaped_dicts.iteritems():
        mmaped_dict.close()
        dst_path = metric_type_to_dst_path[k]
        # Replace existing file:
        shutil.move(mmaped_dict._fname, dst_path)


def _is_alive(pid):
    """Check to see if pid is alive"""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def cleanup_dead_processes(root=None, blocking=True):
    """Cleanup/merge database files from dead processes

    This is not threadsafe and should only be called from one thread/process at
    a time (e.g. a single thread on the multiprocess exporter)

    The blocking argument is mainly used for test purposes. The default
    behavior is to block indefinitely, until lock acquisition. Setting
    blocking=False will immediately raise an exception when acquisition fails
    """
    start_time = time.time()
    if root is None:
        root = _multiproc_dir()
    pids_to_clean = set()
    live_metrics_paths = []

    # Collect all files which belonged to dead workers
    for dirname, _, filenames in os.walk(root):
        for fname in filenames:
            m = _db_pattern.match(fname)
            if not m:
                continue
            name, pid = m.groups()
            pid = int(pid)
            pid_is_alive = _is_alive(pid)
            if pid not in pids_to_clean and not pid_is_alive:
                pids_to_clean.add(pid)
            if pid_is_alive:
                live_metrics_paths.append(os.path.join(dirname, fname))
    lock_type = LOCK_EX if blocking else LOCK_EX | LOCK_NB
    with advisory_lock(lock_type):
        for pid in pids_to_clean:
            logging.info("cleaning up worker %r", pid)
            cleanup_process(pid)
    # TODO: Skip this step if we're using a MultiprocessCollector
    # Merge metrics and cache the results
    archive_paths = filter(os.path.exists, _get_archive_paths(root).values())
    metrics = merge(archive_paths + live_metrics_paths, accumulate=True)
    # TODO: Write time_elapsed into a gauge
    time_elapsed = time.time() - start_time
    _metrics_cache.write_metrics(metrics, time_elapsed)


@contextmanager
def advisory_lock(lock_type, filename="lockfile", prom_dir=None):
    """
    Wrapper around flock.
    The cleanup thread acquires an LOCK_EX
    The metrics collectors acquire LOCK_SH

    The flock interface in python makes it difficult to properly time out lock
    acquisition, and lock acquisition is blocking (a non-blocking lock
    acquisition will immediately fail with an IOError). This should be fine, as
    metrics are not exposed to the wider internet, and gets a predictable, and
    low, request volume. Since they only acquire shared locks, the only
    contention is with the exclusive lock acquired by the cleanup operation,
    which only runs once a minute
    """
    prom_dir = _multiproc_dir() if prom_dir is None else prom_dir
    path = os.path.join(prom_dir, filename)
    with open(path, 'w') as fd:
        flock(fd, lock_type)
        try:
            yield
        finally:
            flock(fd, LOCK_UN)
