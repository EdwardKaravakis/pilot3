#!/usr/bin/env python
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Paul Nilsson, paul.nilsson@cern.ch, 2018-2023

import os
import re
import logging

from pilot.api import analytics
from pilot.util.jobmetrics import get_job_metrics_entry
from pilot.util.features import MachineFeatures, JobFeatures
from pilot.util.filehandling import find_last_line
from pilot.util.math import float_to_rounded_string

from .cpu import get_core_count
from .common import get_db_info, get_resimevents
from .utilities import get_memory_monitor_output_filename

logger = logging.getLogger(__name__)


def get_job_metrics_string(job):
    """
    Get the job metrics string.

    :param job: job object.
    :return: job metrics (string).
    """

    job_metrics = ""

    # report core count (will also set corecount in job object)
    corecount = get_core_count(job)
    logger.debug('job definition core count: %d', corecount)

    #if corecount is not None and corecount != "NULL" and corecount != 'null':
    #    job_metrics += get_job_metrics_entry("coreCount", corecount)

    # report number of actual used cores and add it to the list of measured core counts
    if job.actualcorecount:
        job_metrics += get_job_metrics_entry("actualCoreCount", job.actualcorecount)

    # report number of events
    if job.nevents > 0:
        job_metrics += get_job_metrics_entry("nEvents", job.nevents)
    if job.neventsw > 0:
        job_metrics += get_job_metrics_entry("nEventsW", job.neventsw)

    # add metadata from job report
    if job.metadata:
        job.dbtime, job.dbdata = get_db_info(job.metadata)
        job.resimevents = get_resimevents(job.metadata)
    if job.dbtime and job.dbtime != "":
        job_metrics += get_job_metrics_entry("dbTime", job.dbtime)
    if job.dbdata and job.dbdata != "":
        job_metrics += get_job_metrics_entry("dbData", job.dbdata)
    if job.resimevents is not None:
        job_metrics += get_job_metrics_entry("resimevents", job.resimevents)

    # get the max disk space used by the payload (at the end of a job)
    if job.state == "finished" or job.state == "failed" or job.state == "holding":
        max_space = job.get_max_workdir_size()
        zero = 0

        if max_space > zero:
            job_metrics += get_job_metrics_entry("workDirSize", max_space)
        else:
            logger.info("will not add max space = %d B to job metrics", max_space)

    # add job and machine feature data if available
    job_metrics = add_features(job_metrics, corecount, add=['hs06'])

    # get analytics data
    job_metrics = add_analytics_data(job_metrics, job.workdir, job.state)

    # extract event number from file and add to job metrics if it exists
    job_metrics = add_event_number(job_metrics, job.workdir)

    # add DASK IPs if set
    if job.dask_scheduler_ip and job.jupyter_session_ip:
        job_metrics += get_job_metrics_entry("schedulerIP", job.dask_scheduler_ip)
        job_metrics += get_job_metrics_entry("sessionIP", job.jupyter_session_ip)

    return job_metrics


def add_features(job_metrics, corecount, add=[]):
    """
    Add job and machine feature data to the job metrics if available
    If a non-empty add list is specified, only include the corresponding features. If empty/not specified, add all.

    :param job_metrics: job metrics (string).
    :param corecount: core count (int).
    :param add: features to be added (list).
    :return: updated job metrics (string).
    """

    if job_metrics and not job_metrics.endswith(' '):
        job_metrics += ' '

    def add_sub_features(job_metrics, features_dic, add=[]):
        features_str = ''
        for key in features_dic.keys():
            if add and key not in add:
                continue
            value = features_dic.get(key, None)
            if value:
                features_str += f'{key}={value} '
        return features_str

    machinefeatures = MachineFeatures().get()
    jobfeatures = JobFeatures().get()
    # correct hs06 for corecount: hs06*perf_scale/total_cpu*corecount
    hs06 = machinefeatures.get('hs06', 0)
    total_cpu = machinefeatures.get('total_cpu', 0)
    if hs06 and total_cpu and (total_cpu != '0' or total_cpu != 0):
        perf_scale = 1
        try:
            machinefeatures_hs06 = 1.0 * int(float(hs06)) * perf_scale * corecount / (1.0 * int(float(total_cpu)))
            machinefeatures['hs06'] = float_to_rounded_string(machinefeatures_hs06, precision=2)
            logger.info(f"hs06={machinefeatures.get('hs06')} ({hs06}) total_cpu={total_cpu} corecount={corecount} perf_scale={perf_scale}")
        except (TypeError, ValueError) as exc:
            logger.warning(f'cannot process hs06 machine feature: {exc} (hs06={hs06}, total_cpu={total_cpu}, corecount={corecount})')
    features_list = [machinefeatures, jobfeatures]
    for feature_item in features_list:
        features_str = add_sub_features(job_metrics, feature_item, add=add)
        if features_str:
            job_metrics += features_str

    return job_metrics


def add_analytics_data(job_metrics, workdir, state):
    """
    Add the memory leak+chi2 analytics data to the job metrics.

    :param job_metrics: job metrics (string).
    :param workdir: work directory (string).
    :param state: job state (string).
    :return: updated job metrics (string).
    """

    path = os.path.join(workdir, get_memory_monitor_output_filename())
    if os.path.exists(path):
        client = analytics.Analytics()
        # do not include tails on final update
        tails = False if (state == "finished" or state == "failed" or state == "holding") else True
        data = client.get_fitted_data(path, tails=tails)
        slope = data.get("slope", "")
        chi2 = data.get("chi2", "")
        if slope != "":
            job_metrics += get_job_metrics_entry("leak", slope)
        if chi2 != "":
            job_metrics += get_job_metrics_entry("chi2", chi2)

    return job_metrics


def add_event_number(job_metrics, workdir):
    """
    Extract event number from file and add to job metrics if it exists

    :param job_metrics: job metrics (string).
    :param workdir: work directory (string).
    :return: updated job metrics (string).
    """

    path = os.path.join(workdir, 'eventLoopHeartBeat.txt')
    if os.path.exists(path):
        last_line = find_last_line(path)
        if last_line:
            event_number = get_number_in_string(last_line)
            if event_number:
                job_metrics += get_job_metrics_entry("eventnumber", event_number)
    else:
        logger.debug('file %s does not exist (skip for now)', path)

    return job_metrics


def get_job_metrics(job):
    """
    Return a properly formatted job metrics string.
    The format of the job metrics string is defined by the server. It will be reported to the server during updateJob.

    Example of job metrics:
    Number of events read | Number of events written | vmPeak maximum | vmPeak average | RSS average | ..
    Format: nEvents=<int> nEventsW=<int> vmPeakMax=<int> vmPeakMean=<int> RSSMean=<int> hs06=<float> shutdownTime=<int>
            cpuFactor=<float> cpuLimit=<float> diskLimit=<float> jobStart=<int> memLimit=<int> runLimit=<float>

    :param job: job object.
    :return: job metrics (string).
    """

    # get job metrics string
    job_metrics = get_job_metrics_string(job)

    # correct for potential initial and trailing space
    job_metrics = job_metrics.lstrip().rstrip()

    if job_metrics != "":
        logger.debug('job metrics=\"%s\"', job_metrics)
    else:
        logger.debug("no job metrics (all values are zero)")

    # is job_metrics within allowed size?
    if len(job_metrics) > 500:
        logger.warning("job_metrics out of size (%d)", len(job_metrics))

        # try to reduce the field size and remove the last entry which might be cut
        job_metrics = job_metrics[:500]
        job_metrics = " ".join(job_metrics.split(" ")[:-1])
        logger.warning("job_metrics has been reduced to: %s", job_metrics)

    return job_metrics


def get_number_in_string(line, pattern=r'\ done\ processing\ event\ \#(\d+)\,'):
    """
    Extract a number from the given string.

    E.g. file eventLoopHeartBeat.txt contains
        done processing event #20166959, run #276689 22807 events read so far  <<<===
    This function will return 20166959 as in int.

    :param line: line from a file (string).
    :param pattern: reg ex pattern (raw string).
    :return: extracted number (int).
    """

    event_number = None
    match = re.search(pattern, line)
    if match:
        try:
            event_number = int(match.group(1))
        except (TypeError, ValueError):
            pass

    return event_number
