#!/usr/bin/env python
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Paul Nilsson, paul.nilsson@cern.ch

import os
import re

from pilot.user.atlas.setup import get_asetup
from pilot.user.atlas.setup import get_file_system_root_path
from pilot.info import infosys
from pilot.util.config import config
# import pilot.info.infoservice as infosys

import logging
logger = logging.getLogger(__name__)


def wrapper(executable, **kwargs):
    """
    Wrapper function for any container specific usage.
    This function will be called by pilot.util.container.execute() and prepends the executable with a container command.

    :param executable: command to be executed (string).
    :param kwargs:
    :return: executable wrapped with container command (string).
    """

    workdir = kwargs.get('workdir', '.')
    pilot_home = os.environ.get('PILOT_HOME', '')
    job = kwargs.get('job')

    if workdir == '.' and pilot_home != '':
        workdir = pilot_home

    if config.Container.setup_type == "ALRB":
        fctn = alrb_wrapper
    else:
        fctn = singularity_wrapper
    return fctn(executable, workdir, job)


# def use_payload_container(job):
#     pass


def use_middleware_container():
    """
    Should middleware from container be used?
    In case middleware, i.e. the copy command for stage-in/out, should be taken from a container this function should
    return True.

    :return: True if middleware should be taken from container. False otherwise.
    """

    if get_middleware_type() == 'container':
        return True
    else:
        return False


def get_middleware_container():
    pass


def extract_platform_and_os(platform):
    """
    Extract the platform and OS substring from platform

    :param platform (string): E.g. "x86_64-slc6-gcc48-opt"
    :return: extracted platform specifics (string). E.g. "x86_64-slc6". In case of failure, return the full platform
    """

    pattern = r"([A-Za-z0-9_-]+)-.+-.+"
    a = re.findall(re.compile(pattern), platform)

    if len(a) > 0:
        ret = a[0]
    else:
        logger.warning("could not extract architecture and OS substring using pattern=%s from platform=%s"
                       "(will use %s for image name)" % (pattern, platform, platform))
        ret = platform

    return ret


def get_grid_image_for_singularity(platform):
    """
    Return the full path to the singularity grid image

    :param platform (string): E.g. "x86_64-slc6"
    :return: full path to grid image (string).
    """

    if not platform or platform == "":
        platform = "x86_64-slc6"
        logger.warning("using default platform=%s (cmtconfig not set)" % (platform))

    arch_and_os = extract_platform_and_os(platform)
    image = arch_and_os + ".img"
    _path = os.path.join(get_file_system_root_path(), "atlas.cern.ch/repo/containers/images/singularity")
    path = os.path.join(_path, image)
    if not os.path.exists(path):
        image = 'x86_64-centos7.img'
        logger.warning('path does not exist: %s (trying with image %s instead)' % (path, image))
        path = os.path.join(_path, image)
        if not os.path.exists(path):
            logger.warning('path does not exist either: %s' % path)
            path = ""

    return path


def get_middleware_type():
    """
    Return the middleware type from the container type.
    E.g. container_type = 'singularity:pilot;docker:wrapper;middleware:container'
    get_middleware_type() -> 'container', meaning that middleware should be taken from the container. The default
    is otherwise 'workernode', i.e. middleware is assumed to be present on the worker node.

    :return: middleware_type (string)
    """

    middleware_type = ""
    container_type = infosys.queuedata.container_type

    mw = 'middleware'
    if container_type and container_type != "" and mw in container_type:
        try:
            container_names = container_type.split(';')
            for name in container_names:
                t = name.split(':')
                if mw == t[0]:
                    middleware_type = t[1]
        except Exception as e:
            logger.warning("failed to parse the container name: %s, %s" % (container_type, e))
    else:
        # logger.warning("container middleware type not specified in queuedata")
        # no middleware type was specified, assume that middleware is present on worker node
        middleware_type = "workernode"

    return middleware_type


def alrb_wrapper(cmd, workdir, job):
    """
    Wrap the given command with the special ALRB setup for containers
    E.g. cmd = /bin/bash hello_world.sh
    ->
    export thePlatform="x86_64-slc6-gcc48-opt"
    export ALRB_CONT_RUNPAYLOAD="cmd'
    setupATLAS -c $thePlatform

    :param cmd (string): command to be executed in a container.
    :param workdir: (not used)
    :param job: job object.
    :return: prepended command with singularity execution command (string).
    """

    queuedata = job.infosys.queuedata

    container_name = queuedata.container_type.get("pilot")  # resolve container name for user=pilot
    if container_name == 'singularity':
        # first get the full setup, which should be removed from cmd (or ALRB setup won't work)
        _asetup = get_asetup()
        cmd = cmd.replace(_asetup, "asetup ")
        # get simplified ALRB setup (export)
        asetup = get_asetup(alrb=True)

        # Get the singularity options
        singularity_options = queuedata.container_options
        logger.debug(
            "resolved singularity_options from queuedata.container_options: %s" % singularity_options)

        _cmd = asetup
        _cmd += 'export thePlatform=\"%s\";' % job.platform
        #if '--containall' not in singularity_options:
        #    singularity_options += ' --containall'
        if singularity_options != "":
            _cmd += 'export ALRB_CONT_CMDOPTS=\"%s\";' % singularity_options
        _cmd += 'export ALRB_CONT_RUNPAYLOAD=\"%s\";' % cmd
        _cmd += 'source ${ATLAS_LOCAL_ROOT_BASE}/user/atlasLocalSetup.sh -c images+$thePlatform'
        _cmd = _cmd.replace('  ', ' ')
        cmd = _cmd
        logger.info("Updated command: %s" % cmd)

    return cmd


def singularity_wrapper(cmd, workdir, job):
    """
    Prepend the given command with the singularity execution command
    E.g. cmd = /bin/bash hello_world.sh
    -> singularity_command = singularity exec -B <bindmountsfromcatchall> <img> /bin/bash hello_world.sh
    singularity exec -B <bindmountsfromcatchall>  /cvmfs/atlas.cern.ch/repo/images/singularity/x86_64-slc6.img <script>

    :param cmd (string): command to be prepended.
    :param workdir: explicit work directory where the command should be executed (needs to be set for Singularity).
    :param job: job object.
    :return: prepended command with singularity execution command (string).
    """

    queuedata = job.infosys.queuedata

    container_name = queuedata.container_type.get("pilot")  # resolve container name for user=pilot
    logger.debug("resolved container_name from queuedata.contaner_type: %s" % container_name)

    if container_name == 'singularity':
        logger.info("singularity has been requested")

        # Get the singularity options
        singularity_options = queuedata.container_options
        logger.debug("resolved singularity_options from queuedata.container_options: %s" % singularity_options)

        if not singularity_options:
            logger.warning('singularity options not set')

        # Get the image path
        image_path = get_grid_image_for_singularity(job.platform)

        # Does the image exist?
        if image_path != '':
            # Prepend it to the given command
            cmd = "export workdir=" + workdir + "; singularity exec " + singularity_options + " " + image_path + \
                  " /bin/bash -c \'cd $workdir;pwd;" + cmd.replace("\'", "\\'").replace('\"', '\\"') + "\'"
        else:
            logger.warning("singularity options found but image does not exist")

        logger.info("Updated command: %s" % cmd)

    return cmd
