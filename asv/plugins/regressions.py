# Licensed under a 3-clause BSD style license - see LICENSE.rst
# -*- coding: utf-8 -*-

from __future__ import absolute_import, division, unicode_literals, print_function

import os
import re
import itertools
import multiprocessing
import time
import traceback
import six

from ..console import log
from ..publishing import OutputPublisher
from ..step_detect import detect_regressions

from .. import util


class Regressions(OutputPublisher):
    name = "regressions"
    button_label = "Show regressions"
    description = "Display information about recent regressions"

    @classmethod
    def publish(cls, conf, repo, benchmarks, graphs, hash_to_date):
        # Analyze the data in the graphs --- it's been cleaned up and
        # it's easier to work with than the results directly

        regressions = []
        seen = {}
        date_to_hash = dict((d, h) for h, d in six.iteritems(hash_to_date))

        data_filter = _GraphDataFilter(conf, repo, hash_to_date)

        all_params = {}
        for graph in six.itervalues(graphs):
            for key, value in six.iteritems(graph.params):
                all_params.setdefault(key, set())
                if value:
                    all_params[key].add(value)

        last_percentage_time = time.time()

        n_processes = multiprocessing.cpu_count()
        pool = multiprocessing.Pool(n_processes)
        try:
            results = []
            for j, item in enumerate(six.iteritems(graphs)):
                file_name, graph = item
                if 'summary' in graph.params:
                    continue

                # Print progress status
                log.add('.')
                if time.time() - last_percentage_time > 5:
                    log.add('{0:.0f}%'.format(100*j/len(graphs)))
                    last_percentage_time = time.time()

                benchmark_name = os.path.basename(file_name)
                benchmark = benchmarks.get(benchmark_name)
                if not benchmark:
                    continue

                graph_data = data_filter.get_graph_data(graph, benchmark)
                for data in graph_data:
                    results.append((pool.apply_async(_analyze_data, (data,), {}), graph))

                while len(results) > n_processes:
                    r, graph = results.pop(0)
                    cls._insert_regression(regressions, seen, date_to_hash, repo, all_params,
                                           r.get(), graph)

            while results:
                r, graph = results.pop(0)
                cls._insert_regression(regressions, seen, date_to_hash, repo, all_params,
                                       r.get(), graph)
        finally:
            pool.terminate()

        cls._save(conf, {'regressions': regressions})

    @classmethod
    def _insert_regression(cls, regressions, seen, date_to_hash, repo, all_params,
                           result_item, graph):
        j, entry_name, result = result_item
        if result is None:
            return

        # Check which ranges are a single commit
        jumps = result[0]
        for k, jump in enumerate(jumps):
            commit_a = date_to_hash[jump[0]]
            commit_b = date_to_hash[jump[1]]
            spec = repo.get_range_spec(commit_a, commit_b)
            commits = repo.get_hashes_from_range(spec)
            if len(commits) == 1:
                jumps[k] = (None, jump[1])

        # Select unique graph params
        graph_params = {}
        for name, value in six.iteritems(graph.params):
            if len(all_params[name]) > 1:
                graph_params[name] = value

        graph_path = graph.path + '.json'

        # Produce output -- report only one result for each
        # benchmark for each branch
        regression = [entry_name, graph_path, graph_params, j, result]
        key = (entry_name, graph_params.get('branch'))
        if key not in seen:
            regressions.append(regression)
            seen[key] = regression
        else:
            # Pick the worse regression
            old_regression = seen[key]
            prev_result = old_regression[-1]
            if abs(prev_result[1]*result[2]) < abs(result[1]*prev_result[2]):
                old_regression[:] = regression

    @classmethod
    def _save(cls, conf, data):
        fn = os.path.join(conf.html_dir, 'regressions.json')
        util.write_json(fn, data)


def _analyze_data(graph_data):
    """
    Analyze a single time series

    Returns
    -------
    jumps : list of (time_a, time_b)
         List of time pairs, between which there is an upward jump
         in the value.
    cur_value : int
         Most recent value
    best_value : int
         Best value
    """
    try:
        j, entry_name, times, values = graph_data

        v, jump_pos, best_v = detect_regressions(values)
        if v is None:
            return j, entry_name, None

        jumps = []
        for jump_r in jump_pos:
            for r in range(jump_r + 1, len(values)):
                if values[r] is not None:
                    next_r = r
                    break
            else:
                next_r = jump_r + 1
            jumps.append((times[jump_r], times[next_r]))

        return j, entry_name, (jumps, v, best_v)
    except BaseException as exc:
        raise util.ParallelFailure(str(exc), exc.__class__, traceback.format_exc())


class _GraphDataFilter(object):
    """
    Obtain data sets from graphs, following configuration settings.
    """

    def __init__(self, conf, repo, hash_to_date):
        self.conf = conf
        self.repo = repo
        self.hash_to_date = hash_to_date
        self.time_sets = {}

    def get_graph_data(self, graph, benchmark):
        """
        Iterator over graph data sets

        Yields
        ------
        param_idx
            Flat index to parameter permutations for parameterized benchmarks.
            None if benchmark is not parameterized.
        entry_name
            Name for the data set. If benchmark is non-parameterized, this is the
            benchmark name.
        times
            List of times (ints)
        values
            List of benchmark values (floats or Nones)

        """
        series = graph.get_data()

        if benchmark.get('params'):
            param_iter = enumerate(itertools.product(*benchmark['params']))
        else:
            param_iter = [(None, None)]

        for j, param in param_iter:
            if param is None:
                entry_name = benchmark['name']
            else:
                entry_name = benchmark['name'] + '({0})'.format(', '.join(param))

            time_set = self._get_allowed_times(graph, benchmark, entry_name)

            times = [item[0] for item in series if item[0] in time_set]
            if param is None:
                values = [item[1] for item in series if item[0] in time_set]
            else:
                values = [item[1][j] for item in series if item[0] in time_set]

            yield j, entry_name, times, values

    def _get_allowed_times(self, graph, benchmark, entry_name):
        """
        Compute the set of times allowed by asv.conf.json.

        The decision which commits to include is based on commit
        order, not on commit authoring date
        """
        time_set = set(self.hash_to_date.values())

        if graph.params.get('branch'):
            branch_suffix = '@' + graph.params.get('branch')
        else:
            branch_suffix = ''

        for regex, start_commit in six.iteritems(self.conf.regressions_first_commits):
            if re.match(regex, entry_name + branch_suffix):
                if start_commit is None:
                    # Disable regression detection completely
                    return set()

                if self.conf.branches == [None]:
                    key = (start_commit, None)
                else:
                    key = (start_commit, graph.params.get('branch'))

                if key not in self.time_sets:
                    times = set()
                    spec = self.repo.get_new_range_spec(*key)
                    start_hash = self.repo.get_hash_from_name(start_commit)
                    for commit in [start_hash] + self.repo.get_hashes_from_range(spec):
                        time = self.hash_to_date.get(commit[:self.conf.hash_length])
                        if time is not None:
                            times.add(time)
                    self.time_sets[key] = times

                time_set = time_set.intersection(self.time_sets[key])

        return time_set
