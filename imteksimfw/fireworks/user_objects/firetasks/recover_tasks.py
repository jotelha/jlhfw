#!/usr/bin/env python
#
# recover_tasks.py
#
# Copyright (C) 2020 IMTEK Simulation
# Author: Johannes Hoermann, johannes.hoermann@imtek.uni-freiburg.de
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""Tasks that recover and restart failed computations from checkpoints."""

import collections
import glob
import io
import json
import logging
import os
import shutil

from collections.abc import Iterable
from contextlib import ExitStack

from fireworks.fw_config import FW_LOGGING_FORMAT
from fireworks.utilities.fw_serializers import ENCODING_PARAMS
from fireworks.utilities.dict_mods import get_nested_dict_value, set_nested_dict_value, apply_mod
from fireworks.core.firework import FWAction, FiretaskBase, Firework, Workflow

from imteksimfw.fireworks.utilities.logging import LoggingContext

__author__ = 'Johannes Laurin Hoermann'
__copyright__ = 'Copyright 2020, IMTEK Simulation, University of Freiburg'
__email__ = 'johannes.hoermann@imtek.uni-freiburg.de, johannes.laurin@gmail.com'
__date__ = 'August 10, 2020'

DEFAULT_FORMATTER = logging.Formatter(FW_LOGGING_FORMAT)


def _log_nested_dict(log_func, dct):
    for l in json.dumps(dct, indent=2, default=str).splitlines():
        log_func(l)


# from https://gist.github.com/angstwad/bf22d1822c38a92ec0a9
def dict_merge(dct, merge_dct, add_keys=True,
               exclusions={}, merge_exclusions={}):
    """ Recursive dict merge. Inspired by :meth:``dict.update()``, instead of
    updating only top-level keys, dict_merge recurses down into dicts nested
    to an arbitrary depth, updating keys. The ``merge_dct`` is merged into
    ``dct``.

    This version will return a copy of the dictionary and leave the original
    arguments untouched.

    The optional argument ``add_keys``, determines whether keys which are
    present in ``merge_dct`` but not ``dct`` should be included in the
    new dict.

    Args:
        dct (dict) onto which the merge is executed
        merge_dct (dict): dct merged into dct
        add_keys (bool): whether to add new keys
        exclusions (dict): any such key found within 'dct' will be removed.
                               It can, however, be reintroduced if present in
                               'merge_dct' and 'add_keys' is set.
        merge_exclusions (dict): any such key found within 'merge_dct'
                                     will be ignored. Such keys allready
                                     present within 'dct' are not touched.

    Returns:
        dict: updated dict
    """
    logger = logging.getLogger(__name__)

    merge_dct = merge_dct.copy()
    dct = dct.copy()

    logger.debug("Merge 'merge_dct'...")
    _log_nested_dict(logger.debug, merge_dct)
    logger.debug("... into 'dct'...")
    _log_nested_dict(logger.debug, dct)
    logger.debug("... with 'exclusions'...")
    _log_nested_dict(logger.debug, exclusions)
    logger.debug("... and 'merge_exclusions'...")
    _log_nested_dict(logger.debug, merge_exclusions)

    for k in exclusions:
        if (k in dct) and (dct[k] is True):
            logger.debug("Key '{}' excluded from dct.".format(k))
            del dct[k]

    if not add_keys:
        merge_dct = {
            k: merge_dct[k] for k in set(dct).intersection(set(merge_dct))}
        logger.debug(
            "Not merging keys only in 'merge_dict', only merging {}.".format(
            merge_dct.keys()))

    for k in dct.keys():
        if isinstance(dct[k], dict) and k not in merge_dct:
            merge_dct[k] = {}

    for k, v in merge_dct.items():
        if k in exclusions:
            lower_level_exclusions = exclusions[k]
            logger.debug("Key '{}' included in dct, but exclusions exist for nested keys.".format(k))
        else:
            lower_level_exclusions = {}
            logger.debug("Key '{}' included in dct.".format(k))

        if (k in dct and isinstance(dct[k], dict)
            and isinstance(v, collections.Mapping)):
            if k not in merge_exclusions:  # no exception rule for this field
                logger.debug("Key '{}' included in merge.".format(k))
                dct[k] = dict_merge(dct[k], v, add_keys=add_keys,
                                    exclusions=lower_level_exclusions)
            elif merge_exclusions[k] is not True:  # exception rule for nested fields
                logger.debug("Key '{}' included in merge, but exclusions exist for nested keys.".format(k))
                dct[k] = dict_merge(dct[k], v, add_keys=add_keys,
                                    exclusions=lower_level_exclusions,
                                    merge_exclusions=merge_exclusions[k])
            else:
                logger.debug("Key '{}' excluded from merge.".format(k))
        else:
            if k not in merge_exclusions:  # no exception rule for this field
                logger.debug("Key '{}' included in merge.".format(k))
                dct[k] = v
            else:
                logger.debug("Key '{}' excluded from merge.".format(k))

    return dct


def from_fw_spec(param, fw_spec):
    """Expands param['key'] as key within fw_spec.

    If param is dict hand has field 'key', then return value at specified
    position from fw_spec. Otherwise, return 'param' itself.
    """
    if isinstance(param, dict) and 'key' in param:
        ret = get_nested_dict_value(fw_spec, param['key'])
    else:
        ret = param
    return ret


# we apply update_spec and mod_spec here a priori because additions and detours
# won't be touched by the default mechanism in fireworks.core.firework
def apply_mod_spec(wf, action):
    """Update the spec of the children FireWorks using DictMod language."""
    fw_ids = wf.leaf_fw_ids
    updated_ids = []

    if action.update_spec and action.propagate:
        # Traverse whole sub-workflow down to leaves.
        visited_cfid = set()  # avoid double-updating for diamond deps

        def recursive_update_spec(fw_ids):
            for cfid in fw_ids:
                if cfid not in visited_cfid:
                    visited_cfid.add(cfid)
                    wf.id_fw[cfid].spec.update(action.update_spec)
                    updated_ids.append(cfid)
                    recursive_update_spec(wf.links[cfid])

        recursive_update_spec(fw_ids)
    elif action.update_spec:
        # Update only direct children.
        for cfid in fw_ids:
            wf.id_fw[cfid].spec.update(action.update_spec)
            updated_ids.append(cfid)

    if action.mod_spec and action.propagate:
        visited_cfid = set()

        def recursive_mod_spec(fw_ids):
            for cfid in fw_ids:
                if cfid not in visited_cfid:
                    visited_cfid.add(cfid)
                    for mod in action.mod_spec:
                        apply_mod(mod, wf.id_fw[cfid].spec)
                    updated_ids.append(cfid)
                    recursive_mod_spec(cfid)

        recursive_mod_spec(fw_ids)
    elif action.mod_spec:
        for cfid in fw_ids:
            for mod in action.mod_spec:
                apply_mod(mod, wf.id_fw[cfid].spec)
            updated_ids.append(cfid)

    return updated_ids


class RecoverTask(FiretaskBase):
    """
    Generic base class for recovering and restarting some computation.

    The typical use case is some FIZZLED run, i.e. due to exceeded walltime.
    Activate _allow_fizzled_parents and append to initial run Firework.

    If inserted by means of some 'recovery fw' between a parent and its children

        +--------+     +------------+
        | parent | --> | child(ren) |
        +--------+     +------------+

    as shown

        +--------+     +-------------+     +------------+
        | parent | --> | recovery fw | --> | child(ren) |
        +--------+     +-------------+     +------------+


    then this task generates the following insertion in case of the parent's
    failure

                                                       +- - - - - - - - - - - - - - - -+
                                                       ' detour_wf                     '
                                                       '                               '
                                                       ' +-----------+     +---------+ '
                                 +-------------------> ' |  root(s)  | --> | leaf(s) | ' ------+
                                 |                     ' +-----------+     +---------+ '       |
                                 |                     '                               '       |
                                 |                     +- - - - - - - - - - - - - - - -+       |
                                 |                                                             |
                                 |                                                             |
                                 |                                                             |
                                                       +- - - - - - - - - - - - - - - -+       |
                                                       ' restart_wf                    '       |
                                                       '                               '       v
        +----------------+     +-----------------+     ' +-----------+     +---------+ '     +-----------------+     +------------+
        | fizzled parent | --> | 1st recovery fw | --> ' |  root(s)  | --> | leaf(s) | ' --> | 2nd recovery fw | --> | child(ren) |
        +----------------+     +-----------------+     ' +-----------+     +---------+ '     +-----------------+     +------------+
                                                       '                               '
                                                       +- - - - - - - - - - - - - - - -+
                                 |
                                 |
                                 |
                                 |                     +- - - - - - - - - - - - - - - -+
                                 |                     ' addition_wf                   '
                                 |                     '                               '
                                 |                     ' +-----------+     +---------+ '
                                 +-------------------> ' |  root(s)  | --> | leaf(s) | '
                                                       ' +-----------+     +---------+ '
                                                       '                               '
                                                       +- - - - - - - - - - - - - - - -+

    This dynamoc insertion repeats until 'restart_wf' completes successfully
    or the number of repetitions reaches 'max_restarts'. While 'restart_wf'
    is only appended in the case of a parent's failure, 'detour_wf' and
    'addition_wf' are always inserted:

        + - - - - - - - - - - - -+                              + - - - - - - - - - - - - - - - +
        ' successfull restart_wf '                              ' detour_wf                     '
        '                        '                              '                               '
        '            +---------+ '     +------------------+     ' +-----------+     +---------+ '     +------------+
        '   ...  --> | leaf(s) | ' --> | last recovery fw | --> ' |  root(s)  | --> | leaf(s) | ' --> | child(ren) |
        '            +---------+ '     +------------------+     ' +-----------+     +---------+ '     +------------+
        '                        '                              '                               '
        + - - - - - - - - - - - -+                              + - - - - - - - - - - - - - - - +
                                                           |
                                                           |
                                                           |
                                                           |                      + - - - - - - - - - - - - - - - +
                                                           |                      ' addition_wf                   '
                                                           |                      '                               '
                                                           |                      ' +-----------+     +---------+ '
                                                           +--------------------> ' |  root(s)  | --> | leaf(s) | '
                                                                                  ' +-----------+     +---------+ '
                                                                                  '                               '
                                                                                  + - - - - - - - - - - - - - - - +

    NOTE: make sure that the used 'recovery fw' forwards all outputs
    transparently in case of parent's success.

    NOTE: while the dynamic extensions 'detour_wf' and 'addition_wf' can
    actually be a whole Workflow as well as a single FireWork, 'recover_fw'
    must be single FireWorks. If more complex constructs are necessary,
    consider generating such within those FireWorks.

    NOTE: fails for several parents if the "wrong" parent fizzles. Use only
    in unambiguous situations.

    Required parameters:
        - restart_wf (dict): Workflow or single FireWork to append only if
            restart file present (parent failed). Task will not append anything
            if None. Default: None

    Optional parameters:
        - addition_wf (dict): Workflow or single FireWork to always append as
            an addition, independent on parent's success (i.e. storage).
            Default: None.
        - detour_wf (dict): Workflow or single FireWork to always append as
            a detour, independent on parent's success (i.e. post-processing).
            Task will not append anything if None. Default: None

        - apply_mod_spec_to_addition_wf (bool): Apply FWAction's update_spec and
            mod_spec to 'addition_wf', same as for all other regular childern
            of this task's FireWork. Default: True.
        - apply_mod_spec_to_detour_wf (bool): Apply FWAction's update_spec and
            mod_spec to 'detour_wf', same as for all other regular childern
            of this task's FireWork. Default: True.
        - default_restart_file (str): Name of restart file. Most recent restart
            file found in fizzled parent via 'restart_file_glob_patterns'
            will be copied to new launchdir under this name. Default: None
        - fizzle_on_no_restart_file (bool): Default: True
        - fw_spec_to_exclude ([str]): recover FireWork will inherit the current
            FireWork's 'fw_spec', stripped of top-level fields specified here.
            Default: ['_job_info', '_fw_env', '_files_prev', '_fizzled_parents']
        - ignore_errors (bool): Ignore errors when copying files. Default: True
        - max_restarts (int): Maximum number of repeated restarts (in case of
            'restart' == True). Default: 5
        - other_glob_patterns (str or [str]): Patterns for glob.glob to identify
            other files to be forwarded. All files matching this glob pattern
            are recovered. Default: None
        - repeated_recover_fw_name (str): Name for repeated recovery fireworks.
            If None, the name of this FireWorksis used. Default: None.
        - restart_counter (str): fw_spec path for restart counting.
            Default: 'restart_count'.
        - restart_file_glob_patterns (str or [str]): Patterns for glob.glob to
            identify restart files. Attention: Be careful not to match any
            restart file that has been used as an input initially.
            If more than one file matches a glob pattern in the list, only the
            most recent macth per list entry is recovered.
            Default: ['*.restart[0-9]']
        - superpose_restart_on_parent_fw_spec (bool):
            Try to pull (fizzled) parent's fw_spec and merge with fw_spec of
            all FireWorks within restart_wf, with latter enjoying precedence.
            Default: False.
        - superpose_addition_on_parent_fw_spec (bool):
            Try to pull (fizzled) parent's fw_spec and merge with fw_spec of
            all FireWorks within addition_wf, with latter enjoying precedence.
            Default: False.
        - superpose_detour_on_parent_fw_spec (bool):
            Try to pull (fizzled) parent's fw_spec and merge with fw_spec of
            all FireWorks within detour_wf, with latter enjoying precedence.
            Default: False.
            For above's superpose oprions, fw_spec_to_exclude applies as well.

        - output (str): spec key that will be used to pass output to child
            fireworks. Default: None
        - dict_mod (str, default: '_set'): how to insert output into output
            key, see fireworks.utilities.dict_mods
        - propagate (bool, default: None): if True, then set the
            FWAction 'propagate' flag and propagate updated fw_spec not only to
            direct children, but to all descendants down to wokflow's leaves.
        - stored_data (bool, default: False): put outputs into database via
            FWAction.stored_data
        - store_stdlog (bool, default: False): insert log output into database
            (only if 'stored_data' or 'output' is spcified)
        - stdlog_file (str, Default: NameOfTaskClass.log): print log to file
        - loglevel (str, Default: logging.INFO): loglevel for this task

    Fields 'max_restarts', 'restart_file_glob_patterns', 'other_glob_patterns',
    'default_restart_file', 'fizzle_on_no_restart_file',
    'repeated_recover_fw_name', and 'ignore_errors'
    may also be a dict of format { 'key': 'some->nested->fw_spec->key' } for
    looking up value within 'fw_spec' instead.

    NOTE: reserved fw_spec keywords are
        - all reserved keywords:
        - _tasks
        - _priority
        - _pass_job_info
        - _launch_dir
        - _fworker
        - _category
        - _queueadapter
        - _add_fworker
        - _add_launchpad_and_fw_id
        - _dupefinder
        - _allow_fizzled_parents
        - _preserve_fworker
        - _job_info
        - _fizzled_parents
        - _trackers
        - _background_tasks
        - _fw_env
        - _files_in
        - _files_out
        - _files_prev
    """
    _fw_name = "RecoverLammpsTask"
    required_params = [
        "restart_wf",
    ]
    optional_params = [
        "detour_wf",
        "addition_wf",

        "apply_mod_spec_to_addition_wf",
        "apply_mod_spec_to_detour_wf",
        "fizzle_on_no_restart_file",
        "fw_spec_to_exclude",
        "ignore_errors",
        "max_restarts",
        "other_glob_patterns",
        "repeated_recover_fw_name",
        "restart_counter",
        "restart_file_glob_patterns",
        "restart_file_dests",
        "superpose_restart_on_parent_fw_spec",
        "superpose_addition_on_parent_fw_spec",
        "superpose_detour_on_parent_fw_spec",

        "stored_data",
        "output",
        "dict_mod",
        "propagate",
        "stdlog_file",
        "store_stdlog",
        "loglevel"]

    def appendable_wf_from_dict(self, obj_dict, base_spec=None, exclusions={}):
        """Creates Workflow from a Workflow or single FireWork dict description.

        If specified, use base_spec for all fw_spec and superpose individual
        specs on top."""
        logger = logging.getLogger(__name__)

        logger.debug("Initial obj_dict:")
        _log_nested_dict(logger.debug, obj_dict)

        if base_spec:
            logger.debug("base_spec:")
            _log_nested_dict(logger.debug, base_spec)

        if exclusions:
            logger.debug("exclusions:")
            _log_nested_dict(logger.debug, exclusions)

        if isinstance(obj_dict, dict):
            # in case of single Fireworks:
            if "spec" in obj_dict:
                # append firework (defined as dict):
                if base_spec:
                    obj_dict["spec"] = dict_merge(base_spec, obj_dict["spec"],
                                                  exclusions=exclusions)
                fw = Firework.from_dict(obj_dict)
                fw.fw_id = self.consecutive_fw_id
                self.consecutive_fw_id -= 1
                wf = Workflow([fw])
            else:   # if no single fw, then wf
                if base_spec:
                    for fw_dict in obj_dict["fws"]:
                        fw_dict["spec"] = dict_merge(base_spec, fw_dict["spec"],
                                                     exclusions=exclusions)
                wf = Workflow.from_dict(obj_dict)
                remapped_fw_ids = {}
                # do we have to reassign fw_ids? yes
                for fw in wf.fws:
                    remapped_fw_ids[fw.fw_id] = self.consecutive_fw_id
                    fw.fw_id = self.consecutive_fw_id
                    self.consecutive_fw_id -= 1
                wf._reassign_ids(remapped_fw_ids)
        else:
            raise ValueError("type({}) is '{}', but 'dict' expected.".format(
                             obj_dict, type(obj_dict)))
        logger.debug("Built object:")
        _log_nested_dict(logger.debug, wf.as_dict())

        return wf

    # modeled to match original snippet from fireworks.core.rocket:

    # if my_spec.get("_files_out"):
    #     # One potential area of conflict is if a fw depends on two fws
    #     # and both fws generate the exact same file. That can lead to
    #     # overriding. But as far as I know, this is an illogical use
    #     # of a workflow, so I can't see it happening in normal use.
    #     for k, v in my_spec.get("_files_out").items():
    #         files = glob.glob(os.path.join(launch_dir, v))
    #         if files:
    #             filepath = sorted(files)[-1]
    #             fwaction.mod_spec.append({
    #                 "_set": {"_files_prev->{:s}".format(k): filepath}
    #             })

    # if the curret fw yields outfiles, then check whether according
    # '_files_prev' must be written for newly created insertions
    def write_files_prev(self, wf, fw_spec):
        "Sets _files_prev in roots of new workflow according to _files_out in fw_spec."
        logger = logging.getLogger(__name__)

        if fw_spec.get("_files_out"):
            logger.info("Current FireWork's '_files_out': {}".format(
                        fw_spec.get("_files_out")))

            files_prev = {}

            for k, v in fw_spec.get("_files_out").items():
                files = glob.glob(os.path.join(os.curdir, v))
                if files:
                    logger.info("This Firework provides {}: {}".format(
                                k, files), " within _files_out.")
                    filepath = sorted(files)[-1]
                    logger.info("{}: '{}' provided as '_files_prev'".format(
                                k, filepath), " to subsequent FireWorks.")
                    files_prev[k] = filepath

            # get roots of insertion wf and assign _files_prev to them
            root_fws = [fw for fw in wf.fws if fw.fw_id in wf.root_wf_ids]
            for root_fw in root_fws:
                root_fw.spec["_files_prev"] = files_prev

        return wf


    def run_task(self, fw_spec):
        self.consecutive_fw_id = -1  # quite an ugly necessity
        # get fw_spec entries or their default values:
        restart_wf_dict = self.get('restart_wf', None)
        detour_wf_dict = self.get('detour_wf', None)
        addition_wf_dict = self.get('addition_wf', None)

        apply_mod_spec_to_addition_wf = self.get('apply_mod_spec_to_addition_wf', True)
        apply_mod_spec_to_addition_wf = from_fw_spec(apply_mod_spec_to_addition_wf,
                                                     fw_spec)

        apply_mod_spec_to_detour_wf = self.get('apply_mod_spec_to_detour_wf', True)
        apply_mod_spec_to_detour_wf = from_fw_spec(apply_mod_spec_to_detour_wf,
                                                   fw_spec)

        fizzle_on_no_restart_file = self.get('fizzle_on_no_restart_file', True)
        fizzle_on_no_restart_file = from_fw_spec(fizzle_on_no_restart_file,
                                                 fw_spec)

        ignore_errors = self.get('ignore_errors', True)
        ignore_errors = from_fw_spec(ignore_errors, fw_spec)

        max_restarts = self.get('max_restarts', 5)
        max_restarts = from_fw_spec(max_restarts, fw_spec)

        other_glob_patterns = self.get('other_glob_patterns', None)
        other_glob_patterns = from_fw_spec(other_glob_patterns, fw_spec)

        repeated_recover_fw_name = self.get('repeated_recover_fw_name',
                                            'Repeated LAMMPS recovery')
        repeated_recover_fw_name = from_fw_spec(repeated_recover_fw_name,
                                                fw_spec)

        restart_counter = self.get('restart_counter', 'restart_count')

        restart_file_glob_patterns = self.get('restart_file_glob_patterns',
                                              ['*.restart[0-9]'])
        restart_file_glob_patterns = from_fw_spec(restart_file_glob_patterns,
                                                  fw_spec)

        restart_file_dests = self.get('restart_file_dests', None)
        restart_file_dests = from_fw_spec(restart_file_dests, fw_spec)

        superpose_restart_on_parent_fw_spec = self.get(
            'superpose_restart_on_parent_fw_spec', False)
        superpose_restart_on_parent_fw_spec = from_fw_spec(
            superpose_restart_on_parent_fw_spec, fw_spec)

        superpose_addition_on_parent_fw_spec = self.get(
            'superpose_addition_on_parent_fw_spec', False)
        superpose_addition_on_parent_fw_spec = from_fw_spec(
            superpose_addition_on_parent_fw_spec, fw_spec)

        superpose_detour_on_parent_fw_spec = self.get(
            'superpose_detour_on_parent_fw_spec', False)
        superpose_detour_on_parent_fw_spec = from_fw_spec(
            superpose_detour_on_parent_fw_spec, fw_spec)

        fw_spec_to_exclude = self.get('fw_spec_to_exclude',
                                      [
                                        '_job_info',
                                        '_fw_env',
                                        '_files_prev',
                                        '_fizzled_parents',
                                      ])
        if isinstance(fw_spec_to_exclude, list):
            fw_spec_to_exclude_dict = {k: True for k in fw_spec_to_exclude}
        else:  # supposed to be dict then
            fw_spec_to_exclude_dict = fw_spec_to_exclude

        # generic parameters
        stored_data = self.get('stored_data', False)
        output_key = self.get('output', None)
        dict_mod = self.get('dict_mod', '_set')
        propagate = self.get('propagate', False)

        stdlog_file = self.get('stdlog_file', '{}.log'.format(self._fw_name))
        store_stdlog = self.get('store_stdlog', False)
        loglevel = self.get('loglevel', logging.INFO)

        with ExitStack() as stack:

            if store_stdlog:
                stdlog_stream = io.StringIO()
                logh = logging.StreamHandler(stdlog_stream)
                logh.setFormatter(DEFAULT_FORMATTER)
                stack.enter_context(
                    LoggingContext(handler=logh, level=loglevel, close=False))

            # logging to dedicated log file if desired
            if stdlog_file:
                logfh = logging.FileHandler(
                    stdlog_file, mode='a', **ENCODING_PARAMS)
                logfh.setFormatter(DEFAULT_FORMATTER)
                stack.enter_context(
                    LoggingContext(handler=logfh, level=loglevel, close=True))

            logger = logging.getLogger(__name__)

            # input assertions, ATTENTION: order matters

            # avoid iterating through each character of string
            if isinstance(restart_file_glob_patterns, str):
                restart_file_glob_patterns = [restart_file_glob_patterns]

            if not restart_file_dests:
                # don't rename restart files when recovering
                restart_file_dests = os.curdir

            if isinstance(restart_file_dests, str):
                # if specified as plain string, make it an iterable list
                restart_file_dests = [restart_file_dests]

            if len(restart_file_dests) == 1:
                # if only one nenry, then all possible restart files go to that
                # destination
                restart_file_dests = restart_file_dests*len(
                    restart_file_glob_patterns)

            if len(restart_file_dests) > 1:
                # supposedly, specific destinations have been specified for
                # all possible restart files. If not:
                if len(restart_file_glob_patterns) != len(restart_file_dests):
                    logger.warning(
                        "There are {} restart_file_glob_patterns, "
                        "but {} restart_file_dests, latter ignored. "
                        "Specify none, a single or one "
                        "restart_file_dest per restart_file_glob_patterns "
                        "a Every restart file glob pattern ".format(
                            len(restart_file_glob_patterns),
                            len(restart_file_dests)))
                    # fall back to default
                    restart_file_dests = [os.curdir]*len(
                        restart_file_glob_patterns)

            # we have to decide whether the previous FireWorks failed or ended
            # successfully and then append a restart run or not

            recover = True  # per default, recover
            # check whether a previous firework handed down information
            prev_job_info = None
            path_prefix = None
            # pull from intentionally passed job info:
            if '_job_info' in fw_spec:
                job_info_array = fw_spec['_job_info']
                prev_job_info = job_info_array[-1]
                path_prefix = prev_job_info['launch_dir']
                logger.info('The name of the previous job was: {}'.format(
                    prev_job_info['name']))
                logger.info('The id of the previous job was: {}'.format(
                    prev_job_info['fw_id']))
                logger.info('The location of the previous job was: {}'.format(
                    path_prefix))
            # TODO: fails for several parents if the "wrong" parent fizzles
            # pull from fizzled previous FW:
            elif '_fizzled_parents' in fw_spec:
                fizzled_parents_array = fw_spec['_fizzled_parents']
                # pull latest (or last) fizzled parent:
                prev_job_info = fizzled_parents_array[-1]
                # pull latest launch
                path_prefix = prev_job_info['launches'][-1]['launch_dir']
                logger.info(
                    'The name of fizzled parent Firework was: {}'.format(
                        prev_job_info['name']))
                logger.info(
                    'The id of fizzled parent Firework was: {}'.format(
                        prev_job_info['fw_id']))
                logger.info(
                    'The location of fizzled parent Firework was: {}'.format(
                        path_prefix))
            else:  # no info about previous (fizzled or other) jobs
                logger.info(
                    'No information about previous (fizzled or other) jobs available.')
                recover = False  # don't recover
                # assume that parent completed successfully

            # find other files to forward:
            file_list = []

            if recover:
                if not isinstance(other_glob_patterns, Iterable):
                    other_glob_patterns = [other_glob_patterns]
                for other_glob_pattern in other_glob_patterns:
                    if isinstance(other_glob_pattern, str):  # avoid non string objs
                        logger.info("Processing glob pattern {}".format(
                            other_glob_pattern))
                        file_list.extend(
                            glob.glob(
                                os.path.join(
                                    path_prefix, other_glob_pattern))
                        )

                # copy other files if necessary
                if len(file_list) > 0:
                    for f in file_list:
                        logger.info("File {} will be forwarded.".format(f))
                        try:
                            dest = os.getcwd()
                            shutil.copy(f, dest)
                        except Exception as exc:
                            if ignore_errors:
                                logger.warning("There was an error copying "
                                            "'{}' to '{}', ignored:".format(
                                                f, dest))
                                logger.warning(exc)
                            else:
                                raise exc

                # find restart files as (src, dest) tuples:
                restart_file_list = []

                for glob_pattern, dest in zip(restart_file_glob_patterns,
                                              restart_file_dests):
                    restart_file_matches = glob.glob(os.path.join(
                        path_prefix, glob_pattern))

                    # determine most recent of restart files matches:
                    if len(restart_file_matches) > 1:
                        sorted_restart_file_matches = sorted(
                            restart_file_matches, key=os.path.getmtime)  # sort by modification time
                        logger.info("Several restart files {} (most recent last) "
                                    "for glob pattern '{}'.".format(
                                        glob_pattern,
                                        sorted_restart_file_matches))
                        logger.info("Modification times for those files: {}".format(
                            [os.path.getmtime(f) for f in sorted_restart_file_matches]))
                        logger.info("Most recent restart file '{}' will be copied "
                                    "to '{}'.".format(
                                        sorted_restart_file_matches[-1], dest))
                        restart_file_list.append(
                            (sorted_restart_file_matches[-1], dest))
                    elif len(restart_file_matches) == 1:
                        logger.info("One restart file '{}' for glob "
                                    "pattern '{}' will be copied to '{}'.".format(
                                        restart_file_matches[0],
                                        glob_pattern, dest))
                        restart_file_list.append(
                            (restart_file_matches[0], dest))
                    else:
                        logger.info("No restart file!")
                        if fizzle_on_no_restart_file:
                             raise ValueError(
                                "No restart file in {} for glob pattern {}".format(
                                    path_prefix, glob_pattern))

                # copy all identified restart files
                if len(restart_file_list) > 0:
                    for current_restart_file, dest in restart_file_list:
                        current_restart_file_basename = os.path.basename(current_restart_file)
                        logger.info("File {} will be forwarded.".format(
                            current_restart_file_basename))
                        try:
                            shutil.copy(current_restart_file, dest)
                        except Exception as exc:
                            logger.error("There was an error copying from {} "
                                         "to {}".format(
                                            current_restart_file, dest))
                            raise exc

            # distinguish between FireWorks and Workflows by top-level keys
            # fw: ['spec', 'fw_id', 'created_on', 'updated_on', 'name']
            # wf: ['fws', 'links', 'name', 'metadata', 'updated_on', 'created_on']
            detour_wf = None
            addition_wf = None

            # if detour_fw given, append in any case:
            if isinstance(detour_wf_dict, dict):
                detour_wf_base_spec = None
                if superpose_detour_on_parent_fw_spec:
                    if "spec" in prev_job_info:
                        detour_wf_base_spec = prev_job_info["spec"]
                    else:
                        logger.warning("Superposition of detour_wf's "
                                       "fw_spec onto parent's "
                                       "fw_spec desired, but not parent"
                                       "fw_spec recovered.")
                detour_wf = self.appendable_wf_from_dict(
                    detour_wf_dict, base_spec=detour_wf_base_spec,
                    exclusions=fw_spec_to_exclude_dict)

            if detour_wf is not None:
                logger.debug(
                    "detour_wf:")
                _log_nested_dict(logger.debug, detour_wf.as_dict())

            # append restart fireworks if desired
            if recover:
                # try to derive number of restart from fizzled parent
                restart_count = None
                if prev_job_info and ('spec' in prev_job_info):
                    try:
                        restart_count = get_nested_dict_value(
                            prev_job_info['spec'], restart_counter)
                    except KeyError:
                        logger.warning("Found no restart count in fw_spec of "
                                       "fizzled parent at key '{}.'".format(
                                            restart_counter))

                # if none found, look in own fw_spec
                if restart_count is None:
                    try:
                        restart_count = get_nested_dict_value(
                            prev_job_info['spec'], restart_counter)
                    except KeyError:
                        logger.warning("Found no restart count in own fw_spec "
                                       "at key '{}.'".format(restart_counter))

                # if still none found, assume it's the "0th"
                if restart_count is None:
                    restart_count = 0
                else:  # make sure above's queried value is an integer
                    restart_count = int(restart_count) + 1

                if restart_count < max_restarts + 1:
                    logger.info(
                        "This is #{:d} of at most {:d} restarts.".format(
                            restart_count+1, max_restarts))

                    restart_wf_base_spec = None
                    if superpose_restart_on_parent_fw_spec:
                        if "spec" in prev_job_info:
                            restart_wf_base_spec = prev_job_info["spec"]
                        else:
                            logger.warning("Superposition of restart_wf's "
                                           "fw_spec onto parent's "
                                           "fw_spec desired, but not parent"
                                           "fw_spec recovered.")
                    restart_wf = self.appendable_wf_from_dict(
                        restart_wf_dict, base_spec=restart_wf_base_spec,
                        exclusions=fw_spec_to_exclude_dict)

                    # apply updates to fw_spec
                    for fws in restart_wf.fws:
                        set_nested_dict_value(
                            fws.spec, restart_counter, restart_count)

                    logger.debug(
                        "restart_wf:")
                    _log_nested_dict(logger.debug, restart_wf.as_dict())

                    # repeatedly append copy of this recover task:
                    recover_ft = self
                    logger.debug("subsequent recover_fw's task recover_ft:")
                    _log_nested_dict(logger.debug, recover_ft.as_dict())

                    # repeated recovery firework inherits the following specs:
                    # recover_fw_spec = {key: fw_spec[key] for key in fw_spec
                    #                   if key not in fw_spec_to_exclude}
                    recover_fw_spec = dict_merge({}, fw_spec,
                                                 exclusions=fw_spec_to_exclude_dict)
                    logger.debug("propagating fw_spec = {} to subsequent "
                                 "recover_fw.".format(recover_fw_spec))

                    # merge insertions
                    #
                    #  + - - - - - - - - - - - - - - - - - - -+
                    #  ' detour_wf                            '
                    #  '                                      '
                    #  ' +------------------+     +---------+ '
                    #  ' | detour_wf roots  | --> | leaf(s) | ' ------+
                    #  ' +------------------+     +---------+ '       |
                    #  '                                      '       |
                    #  + - - - - - - - - - - - - - - - - - - -+       |
                    #                                                 |
                    #                                                 |
                    #                                                 |
                    #  + - - - - - - - - - - - - - - - - - - -+       |
                    #  ' restart_wf                           '       |
                    #  '                                      '       v
                    #  ' +------------------+     +---------+ '     +----------+
                    #  ' | restart_wf roots | --> | leaf(s) | ' --> | recovery |
                    #  ' +------------------+     +---------+ '     +----------+
                    #
                    # into one workflow and make repeated recovery fireworks
                    # dependent on all leaf fireworks of detour and restart:

                    if restart_wf is not None and detour_wf is not None:
                        detour_wf.append_wf(restart_wf, [])
                    elif restart_wf is not None:  # and detour wf is None
                        detour_wf = restart_wf

                    recover_fw = Firework(
                        recover_ft,
                        spec=recover_fw_spec,  # inherit this Firework's spec
                        name=repeated_recover_fw_name,
                        fw_id=self.consecutive_fw_id)
                    self.consecutive_fw_id -= 1
                    logger.info("Create repeated recover Firework {} with "
                                "id {} and specs {}".format(recover_fw.name,
                                                            recover_fw.fw_id,
                                                            recover_fw.spec))

                    recover_wf = Workflow([recover_fw])
                    detour_wf.append_wf(recover_wf, detour_wf.leaf_fw_ids)

                    logger.debug(
                        "Workflow([*detour_wf.fws, *restart_wf.fws, recover_fw]):")
                    _log_nested_dict(logger.debug, detour_wf.as_dict())
                else:
                    logger.warning(
                        "Maximum number of {} restarts reached. "
                        "No further restart.".format(max_restarts))

                self.write_files_prev(detour_wf, fw_spec)
            else:
                logger.warning("No restart Fireworks appended.")

            if isinstance(addition_wf_dict, dict):
                addition_wf_base_spec = None
                if superpose_addition_on_parent_fw_spec:
                    if "spec" in prev_job_info:
                        addition_wf_base_spec = prev_job_info["spec"]
                    else:
                        logger.warning("Superposition of addition_wf's "
                                       "fw_spec onto parent's "
                                       "fw_spec desired, but not parent"
                                       "fw_spec recovered.")
                addition_wf = self.appendable_wf_from_dict(
                    addition_wf_dict, base_spec=addition_wf_base_spec,
                    exclusions=fw_spec_to_exclude_dict)
                self.write_files_prev(addition_wf, fw_spec)

        # end of ExitStack context
        output = {}
        if store_stdlog:
            stdlog_stream.flush()
            output['stdlog'] = stdlog_stream.getvalue()

        fw_action = FWAction()

        if stored_data:
            fw_action.stored_data = output

        if hasattr(fw_action, 'propagate') and propagate:
            fw_action.propagate = propagate

        if output_key:  # inject into fw_spec
            fw_action.mod_spec = [{dict_mod: {output_key: output}}]

        if addition_wf and apply_mod_spec_to_addition_wf:
            apply_mod_spec(addition_wf, fw_action)

        if detour_wf and apply_mod_spec_to_detour_wf:
            apply_mod_spec(detour_wf, fw_action)

        if addition_wf:
            fw_action.additions = [addition_wf]

        if detour_wf:
            fw_action.detours = [detour_wf]

        return fw_action
