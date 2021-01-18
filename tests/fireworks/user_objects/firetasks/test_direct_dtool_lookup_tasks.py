# coding: utf-8
"""Test dtool lookup server queries integration."""

__author__ = 'Johannes Laurin Hoermann'
__copyright__ = 'Copyright 2020, IMTEK Simulation, University of Freiburg'
__email__ = 'johannes.hoermann@imtek.uni-freiburg.de, johannes.laurin@gmail.com'
__date__ = 'Jan 18, 2021'

import glob
import json
import logging
import pytest

from fireworks.utilities.dict_mods import apply_mod

from imteksimfw.utils.logging import _log_nested_dict

from imteksimfw.fireworks.user_objects.firetasks.dataflow_tasks import SearchDictTask
from imteksimfw.fireworks.user_objects.firetasks.dtool_tasks import FetchItemTask
from imteksimfw.fireworks.user_objects.firetasks.dtool_lookup_tasks import (
    DirectReadmeTask, DirectManifestTask)
from imteksimfw.utils.dict import compare, _make_marker

# from test_dtool_tasks import _compare


@pytest.fixture
def dtool_smb_config(dtool_config):
    """Provide default dtool lookup config."""
    dtool_config.update({
        "DTOOL_SMB_SERVER_NAME_test-share": "localhost",
        "DTOOL_SMB_SERVER_PORT_test-share": 4445,
        "DTOOL_SMB_USERNAME_test-share": "guest",
        "DTOOL_SMB_PASSWORD_test-share": "a-guest-needs-no-password",
        "DTOOL_SMB_DOMAIN_test-share": "WORKGROUP",
        "DTOOL_SMB_SERVICE_NAME_test-share": "sambashare",
        "DTOOL_SMB_PATH_test-share": "dtool"
    })
    return dtool_config


@pytest.fixture
def default_direct_readme_dtool_task_spec(dtool_smb_config):
    """Provide default test task_spec for ReadmeDtoolTask."""
    return {
        'dtool_config': dtool_smb_config,
        'stored_data': True,
        'uri': 'smb://test-share/1a1f9fad-8589-413e-9602-5bbd66bfe675',
        'loglevel': logging.DEBUG,
    }


@pytest.fixture
def default_direct_manifest_dtool_task_spec(dtool_smb_config):
    """Provide default test task_spec for ManifestDtoolTask."""
    return {
        'dtool_config': dtool_smb_config,
        'stored_data': True,
        'uri': 'smb://test-share/1a1f9fad-8589-413e-9602-5bbd66bfe675',
        'loglevel': logging.DEBUG,
    }


#
# direct dtool lookup tasks tests
#


def test_direct_readme_dtool_task_run(default_direct_readme_dtool_task_spec):
    """Will lookup some dataset on the server."""
    logger = logging.getLogger(__name__)

    logger.debug("Instantiate DirectReadmeTask with '{}'".format(
        default_direct_readme_dtool_task_spec))

    t = DirectReadmeTask(**default_direct_readme_dtool_task_spec)
    fw_action = t.run_task({})
    logger.debug("FWAction:")
    _log_nested_dict(logger.debug, fw_action.as_dict())

    output = fw_action.stored_data['output']

    # TODO: dataset creation in test
    expected_respone = {
        "creation_date": "2020-11-08",
        "description": "testing description",
        "expiration_date": "2022-11-08",
        "funders": [
          {
            "code": "testing_code",
            "organization": "testing_organization",
            "program": "testing_program"
          }
        ],
        "owners": [
          {
            "email": "testing@test.edu",
            "name": "Testing User",
            "orcid": "testing_orcid",
            "username": "testing_user"
          }
        ],
        "project": "testing project"
    }

    assert compare(output, expected_respone)


def test_direct_manifest_dtool_task_run(default_direct_manifest_dtool_task_spec):
    """Will lookup some dataset on the server."""
    logger = logging.getLogger(__name__)

    logger.debug("Instantiate ManifestDtoolTask with '{}'".format(
        default_direct_manifest_dtool_task_spec))

    t = DirectManifestTask(**default_direct_manifest_dtool_task_spec)
    fw_action = t.run_task({})
    logger.debug("FWAction:")
    _log_nested_dict(logger.debug, fw_action.as_dict())

    output = fw_action.stored_data['output']

    # TODO: dataset creation in test
    expected_respone = {
        "dtoolcore_version": "3.18.0",
        "hash_function": "md5sum_hexdigest",
        "items": {
            "eb58eb70ebcddf630feeea28834f5256c207edfd": {
                "hash": "2f7d9c3e0cfd47e8fcab0c12447b2bf0",
                "relpath": "simple_text_file.txt",
                "size_in_bytes": 17,
                "utc_timestamp": 1606595093.53965
             }
        }
    }

    marker = {
        "dtoolcore_version": False,
        "hash_function": "md5sum_hexdigest",
        "items": {
            "eb58eb70ebcddf630feeea28834f5256c207edfd": {
                "hash": True,
                "relpath": True,
                "size_in_bytes": True,
                "utc_timestamp": False,
             }
        }
    }

    assert compare(output, expected_respone, marker)


# complex workflow

@pytest.fixture
def workflow_initial_fw_spec(dtool_smb_config):
    """Provide complex workflow test task_spec for QueryDtoolTask."""
    return {
        'initial_inputs': {
            'search': {
                'relpath': 'simple_text_file.txt',
            },
            'marker': {
                'relpath': True,
            },
        },
        'metadata': {
            "creation_date": "2020-11-28",
            "description": "description to override",
            "project": "derived testing project"
        }
    }


@pytest.fixture
def workflow_direct_readme_dtool_task_spec(dtool_smb_config):
    """Provide complex workflow test task_spec for QueryDtoolTask."""
    return {
        'dtool_config': dtool_smb_config,
        'stored_data': True,
        'uri': 'smb://test-share/1a1f9fad-8589-413e-9602-5bbd66bfe675',
        'output': 'metadata',
        'metadata_fw_source_key': 'metadata',
        'fw_supersedes_dtool': True,
        'metadata_dtool_exclusions': {'expiration_date': True},
        "metadata_fw_exclusions": {'description': True},
        'loglevel': logging.DEBUG
    }


@pytest.fixture
def workflow_direct_manifest_dtool_task_spec(dtool_smb_config):
    """Provide complex workflow test task_spec for ManifestDtoolTask."""
    return {
        'dtool_config': dtool_smb_config,
        'stored_data': True,
        'uri': 'smb://test-share/1a1f9fad-8589-413e-9602-5bbd66bfe675',
        'output': 'manifest_dtool_task->result',
        'loglevel': logging.DEBUG
    }


@pytest.fixture
def workflow_search_dict_task_spec():
    """Provide complex workflow test task_spec for SearchDictTask."""
    return {
        'input_key': 'manifest_dtool_task->result->items',
        'search_key': 'initial_inputs->search',
        'marker_key': 'initial_inputs->marker',
        'limit': 1,
        'expand': True,
        'stored_data': True,
        'output_key': 'search_dict_task->result',
        'loglevel': logging.DEBUG
    }


@pytest.fixture
def workflow_fetch_item_task_spec(dtool_smb_config):
    """Provide default test task_spec for CopyDatasetTask."""
    return {
        'item_id': {'key': 'search_dict_task->result'},
        'source': 'smb://test-share/1a1f9fad-8589-413e-9602-5bbd66bfe675',
        'filename': 'fetched_item.txt',
        'dtool_config': dtool_smb_config,
        'stored_data': True,
    }


def test_complex_direct_dtool_workflow(
        tempdir, workflow_initial_fw_spec,
        workflow_direct_readme_dtool_task_spec, workflow_direct_manifest_dtool_task_spec,
        workflow_search_dict_task_spec, workflow_fetch_item_task_spec,
        dtool_smb_config):
    """Query lookup server for datasets, subsequently query the readme, merge into fw_spec, then fetch specific file."""
    logger = logging.getLogger(__name__)

    # readme

    fw_spec = workflow_initial_fw_spec

    t = DirectReadmeTask(workflow_direct_readme_dtool_task_spec)
    logger.debug("Instantiated ReadmeDtoolTask as:")
    _log_nested_dict(logger.debug, t.as_dict())

    fw_action = t.run_task(fw_spec)
    logger.debug("FWAction:")
    _log_nested_dict(logger.debug, fw_action.as_dict())

    for mod in fw_action.mod_spec:
        apply_mod(mod, fw_spec)
    logger.debug("Modified fw_spec:")
    _log_nested_dict(logger.debug, fw_spec)
    
    expected_result = {
        "creation_date": "2020-11-28",  # from initial fw_spec
        "description": "testing description",  # from dtool readme
        # no "expiration_date" from dtool readme
        "funders": [
          {
            "code": "testing_code",
            "organization": "testing_organization",
            "program": "testing_program"
          }
        ],
        "owners": [
          {
            "email": "testing@test.edu",
            "name": "Testing User",
            "orcid": "testing_orcid",
            "username": "testing_user"
          }
        ],
        "project": "derived testing project",  # from fw_spec
    }
    marker = _make_marker(expected_result)

    assert compare(fw_spec['metadata'], expected_result, marker)

    # manifest

    t = DirectManifestTask(workflow_direct_manifest_dtool_task_spec)
    logger.debug("Instantiated ManifestDtoolTask as:")
    _log_nested_dict(logger.debug, t.as_dict())

    fw_action = t.run_task(fw_spec)
    logger.debug("FWAction:")
    _log_nested_dict(logger.debug, fw_action.as_dict())

    for mod in fw_action.mod_spec:
        apply_mod(mod, fw_spec)
    logger.debug("Modified fw_spec:")
    _log_nested_dict(logger.debug, fw_spec)

    expected_result = {
        "dtoolcore_version": "3.18.0",
        "hash_function": "md5sum_hexdigest",
        "items": {
            "eb58eb70ebcddf630feeea28834f5256c207edfd": {
                "hash": "2f7d9c3e0cfd47e8fcab0c12447b2bf0",
                "relpath": "simple_text_file.txt",
                "size_in_bytes": 17,
                "utc_timestamp": 1606595093.53965
             }
        }
    }

    marker = {
        "dtoolcore_version": False,
        "hash_function": True,
        "items": {
            "eb58eb70ebcddf630feeea28834f5256c207edfd": {
                "hash": True,
                "relpath": True,
                "size_in_bytes": True,
                "utc_timestamp": False,
             }
        }
    }

    assert compare(fw_spec['manifest_dtool_task']['result'], expected_result, marker)

    # search

    t = SearchDictTask(workflow_search_dict_task_spec)
    logger.debug("Instantiated SearchDictTask as:")
    _log_nested_dict(logger.debug, t.as_dict())

    fw_action = t.run_task(fw_spec)
    logger.debug("FWAction:")
    _log_nested_dict(logger.debug, fw_action.as_dict())

    for mod in fw_action.mod_spec:
        apply_mod(mod, fw_spec)
    logger.debug("Modified fw_spec:")
    _log_nested_dict(logger.debug, fw_spec)

    # fetch

    t = FetchItemTask(workflow_fetch_item_task_spec)
    fw_action = t.run_task(fw_spec)
    logger.debug("FWAction:")
    _log_nested_dict(logger.debug, fw_action.as_dict())

    local_item_files = glob.glob('*.txt')
    assert len(local_item_files)  == 1
    assert local_item_files[0] == 'fetched_item.txt'
    with open('fetched_item.txt', 'r') as f:
        content = f.read ()
    assert content == 'Some test content'

