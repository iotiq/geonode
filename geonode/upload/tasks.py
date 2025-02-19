#########################################################################
#
# Copyright (C) 2021 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################
import json
import logging

from celery import chord
from gsimporter.api import NotFound

from django.conf import settings
from django.utils.timezone import timedelta, now

from geonode.celery_app import app
from geonode.base import enumerations
from geonode.geoserver.helpers import gs_uploader

from geonode.upload.models import Upload
from geonode.upload.views import final_step_view
from geonode.upload.utils import next_step_response
from geonode.resource.manager import resource_manager

from geonode.tasks.tasks import (
    AcquireLock,
    FaultTolerantTask)

logger = logging.getLogger(__name__)

UPLOAD_SESSION_EXPIRY_HOURS = getattr(settings, 'UPLOAD_SESSION_EXPIRY_HOURS', 24)


@app.task(
    bind=True,
    base=FaultTolerantTask,
    queue='upload',
    acks_late=False,
    ignore_result=False,
)
def finalize_incomplete_session_uploads(self, *args, **kwargs):
    """The task periodically checks for pending and stale Upload sessions.
    It runs every 600 seconds (see the PeriodTask on geonode.upload._init_),
    checks first for expired stale Upload sessions and schedule them for cleanup.
    We have to make sure To NOT Delete those Unprocessed Ones,
    which are in live sessions.
    After removing the stale ones, it collects all the unprocessed and runs them
    in parallel."""

    lock_id = f'{self.request.id}'
    with AcquireLock(lock_id) as lock:
        if lock.acquire() is True:
            _upload_ids = []
            _upload_tasks = []

            # Check first if we need to delete stale sessions
            expiry_time = now() - timedelta(hours=UPLOAD_SESSION_EXPIRY_HOURS)
            for _upload in Upload.objects.exclude(state=enumerations.STATE_PROCESSED).exclude(date__gt=expiry_time):
                _upload.set_processing_state(enumerations.STATE_INVALID)
                _upload_ids.append(_upload.id)
                _upload_tasks.append(
                    _upload_session_cleanup.signature(
                        args=(_upload.id,)
                    )
                )

            upload_workflow_finalizer = _upload_workflow_finalizer.signature(
                args=('_upload_session_cleanup', _upload_ids,),
                immutable=True
            ).on_error(
                _upload_workflow_error.signature(
                    args=('_upload_session_cleanup', _upload_ids,),
                    immutable=True
                )
            )
            upload_workflow = chord(_upload_tasks, body=upload_workflow_finalizer)
            upload_workflow.apply_async()

            # Let's finish the valid ones
            _processing_states = (
                enumerations.STATE_RUNNING,
                enumerations.STATE_INVALID,
                enumerations.STATE_PROCESSED)
            for _upload in Upload.objects.exclude(state__in=_processing_states):
                session = None
                try:
                    if not _upload.import_id:
                        raise NotFound
                    session = _upload.get_session.import_session
                    if not session or session.state != enumerations.STATE_COMPLETE:
                        session = gs_uploader.get_session(_upload.import_id)
                except (NotFound, Exception) as e:
                    logger.exception(e)
                    session = None
                    if _upload.state not in (enumerations.STATE_COMPLETE, enumerations.STATE_PROCESSED):
                        _upload.set_processing_state(enumerations.STATE_INVALID)
                        if _upload.resource:
                            resource_manager.delete(_upload.resource.uuid)

                if session:
                    _upload_ids.append(_upload.id)
                    _upload_tasks.append(
                        _update_upload_session_state.signature(
                            args=(_upload.id,)
                        )
                    )

            upload_workflow_finalizer = _upload_workflow_finalizer.signature(
                args=('_update_upload_session_state', _upload_ids,),
                immutable=True
            ).on_error(
                _upload_workflow_error.signature(
                    args=('_update_upload_session_state', _upload_ids,),
                    immutable=True
                )
            )
            upload_workflow = chord(_upload_tasks, body=upload_workflow_finalizer)
            upload_workflow.apply_async()


@app.task(
    bind=True,
    base=FaultTolerantTask,
    queue='upload',
    acks_late=False,
    ignore_result=False,
)
def _upload_workflow_finalizer(self, task_name: str, upload_ids: list):
    """Task invoked at 'upload_workflow.chord' end in the case everything went well.
    """
    logger.info(f"Task {task_name} upload ids: {upload_ids} finished successfully!")


@app.task(
    bind=True,
    base=FaultTolerantTask,
    queue='upload',
    acks_late=False,
    ignore_result=False,
)
def _upload_workflow_error(self, task_name: str, upload_ids: list):
    """Task invoked at 'upload_workflow.chord' end in the case some error occurred.
    """
    logger.error(f"Task {task_name} upload ids: {upload_ids} did not finish correctly!")


@app.task(
    bind=True,
    base=FaultTolerantTask,
    queue='upload',
    acks_late=False,
    ignore_result=False,
)
def _update_upload_session_state(self, upload_session_id: int):
    """Task invoked by 'upload_workflow.chord' in order to process all the 'PENDING' Upload tasks."""

    lock_id = f'{self.request.id}'
    with AcquireLock(lock_id) as lock:
        if lock.acquire() is True:
            _upload = Upload.objects.get(id=upload_session_id)
            session = _upload.get_session.import_session
            if not session or session.state != enumerations.STATE_COMPLETE:
                session = gs_uploader.get_session(_upload.import_id)

            if session:
                try:
                    content = next_step_response(None, _upload.get_session).content
                    if isinstance(content, bytes):
                        content = content.decode('UTF-8')
                    response_json = json.loads(content)
                    _success = response_json.get('success', False)
                    _redirect_to = response_json.get('redirect_to', '')
                    if _success:
                        if 'upload/final' not in _redirect_to and 'upload/check' not in _redirect_to:
                            _upload.set_resume_url(_redirect_to)
                            _upload.set_processing_state(enumerations.STATE_WAITING)
                        else:
                            if session.state == enumerations.STATE_COMPLETE and _upload.state == enumerations.STATE_PENDING:
                                if not _upload.resource or not _upload.resource.processed:
                                    final_step_view(None, _upload.get_session)
                                _upload.set_processing_state(enumerations.STATE_RUNNING)
                except (NotFound, Exception) as e:
                    logger.exception(e)
                    if _upload.state not in (enumerations.STATE_COMPLETE, enumerations.STATE_PROCESSED):
                        _upload.set_processing_state(enumerations.STATE_INVALID)
                        if _upload.resource:
                            resource_manager.delete(_upload.resource.uuid)


@app.task(
    bind=True,
    base=FaultTolerantTask,
    queue='upload',
    acks_late=False,
    ignore_result=False,
)
def _upload_session_cleanup(self, upload_session_id: int):
    """Task invoked by 'upload_workflow.chord' in order to remove and cleanup all the 'INVALID' stale Upload tasks."""

    lock_id = f'{self.request.id}'
    with AcquireLock(lock_id) as lock:
        if lock.acquire() is True:
            try:
                _upload = Upload.objects.get(id=upload_session_id)
                if _upload.resource:
                    resource_manager.delete(_upload.resource.uuid)
                _upload.delete()
                logger.debug(f"Upload {upload_session_id} deleted with state {_upload.state}.")
            except Exception as e:
                logger.error(f"Upload {upload_session_id} errored with exception {e}.")
