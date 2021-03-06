# Copyright (c) 2013 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import re

from eventlet.green import subprocess
from oslo_log import log as logging

from trove.common import cfg
from trove.common.i18n import _
from trove.common import stream_codecs
from trove.guestagent.common import operating_system
from trove.guestagent.common.operating_system import FileMode
from trove.guestagent.datastore.experimental.postgresql.service import PgSqlApp
from trove.guestagent.strategies.restore import base

CONF = cfg.CONF
LOG = logging.getLogger(__name__)
WAL_ARCHIVE_DIR = CONF.postgresql.wal_archive_location


class PgDump(base.RestoreRunner):
    """Implementation of Restore Strategy for pg_dump."""
    __strategy_name__ = 'pg_dump'
    base_restore_cmd = 'psql -U os_admin'

    IGNORED_ERROR_PATTERNS = [
        re.compile("ERROR:\s*role \"postgres\" already exists"),
    ]

    def restore(self):
        """We are overriding the base class behavior
        to perform custom error handling.
        """
        self.pre_restore()
        content_length = self._execute_postgres_restore()
        self.post_restore()
        return content_length

    def _execute_postgres_restore(self):
        # Postgresql outputs few benign messages into the stderr stream
        # during a normal restore procedure.
        # We need to watch for those and avoid raising
        # an exception in response.
        # Message 'ERROR:  role "postgres" already exists'
        # is expected and does not pose any problems to the restore operation.

        stream = self.storage.load(self.location, self.checksum)
        process = subprocess.Popen(self.restore_cmd, shell=True,
                                   stdin=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        content_length = 0
        for chunk in stream:
            process.stdin.write(chunk)
            content_length += len(chunk)
        process.stdin.close()
        self._handle_errors(process)
        LOG.info(_("Restored %s bytes from stream."), content_length)

        return content_length

    def _handle_errors(self, process):
        # Handle messages in the error stream of a given process.
        # Raise an exception if the stream is not empty and
        # does not match the expected message sequence.

        try:
            err = process.stderr.read()
            # Empty error stream is always accepted as valid
            # for future compatibility.
            if err:
                for message in err.splitlines(False):
                    if not any(regex.match(message)
                               for regex in self.IGNORED_ERROR_PATTERNS):
                        raise Exception(message)
        except OSError:
            pass


class PgBaseBackup(base.RestoreRunner):
    """Implementation of Restore Strategy for pg_basebackup."""
    __strategy_name__ = 'pg_basebackup'
    location = ""
    base_restore_cmd = ''

    IGNORED_ERROR_PATTERNS = [
        re.compile("ERROR:\s*role \"postgres\" already exists"),
    ]

    def __init__(self, *args, **kwargs):
        self._app = None
        self.base_restore_cmd = 'sudo -u %s tar xCf %s - ' % (
            self.app.pgsql_owner, self.app.pgsql_data_dir
        )

        super(PgBaseBackup, self).__init__(*args, **kwargs)

    @property
    def app(self):
        if self._app is None:
            self._app = self._build_app()
        return self._app

    def _build_app(self):
        return PgSqlApp()

    def pre_restore(self):
        self.app.stop_db()
        LOG.info(_("Preparing WAL archive dir"))
        self.app.recreate_wal_archive_dir()
        datadir = self.app.pgsql_data_dir
        operating_system.remove(datadir, force=True, recursive=True,
                                as_root=True)
        operating_system.create_directory(datadir, user=self.app.pgsql_owner,
                                          group=self.app.pgsql_owner,
                                          force=True, as_root=True)

    def post_restore(self):
        operating_system.chmod(self.app.pgsql_data_dir,
                               FileMode.SET_USR_RWX(),
                               as_root=True, recursive=True, force=True)

    def write_recovery_file(self, restore=False):
        metadata = self.storage.load_metadata(self.location, self.checksum)
        recovery_conf = ""
        recovery_conf += "recovery_target_name = '%s' \n" % metadata['label']
        recovery_conf += "recovery_target_timeline = '%s' \n" % 1

        if restore:
            recovery_conf += ("restore_command = '" +
                              self.pgsql_restore_cmd + "'\n")

        recovery_file = os.path.join(self.app.pgsql_data_dir, 'recovery.conf')
        operating_system.write_file(recovery_file, recovery_conf,
                                    codec=stream_codecs.IdentityCodec(),
                                    as_root=True)
        operating_system.chown(recovery_file, user=self.app.pgsql_owner,
                               group=self.app.pgsql_owner, as_root=True)


class PgBaseBackupIncremental(PgBaseBackup):

    def __init__(self, *args, **kwargs):
        super(PgBaseBackupIncremental, self).__init__(*args, **kwargs)
        self.content_length = 0
        self.incr_restore_cmd = 'sudo -u %s tar -xf - -C %s ' % (
                                self.app.pgsql_owner, WAL_ARCHIVE_DIR
        )
        self.pgsql_restore_cmd = "cp " + WAL_ARCHIVE_DIR + '/%f "%p"'

    def pre_restore(self):
        self.app.stop_db()

    def post_restore(self):
        self.write_recovery_file(restore=True)

    def _incremental_restore_cmd(self, incr=False):
        args = {'restore_location': self.restore_location}
        cmd = self.base_restore_cmd
        if incr:
            cmd = self.incr_restore_cmd
        return self.decrypt_cmd + self.unzip_cmd + (cmd % args)

    def _incremental_restore(self, location, checksum):

        metadata = self.storage.load_metadata(location, checksum)
        if 'parent_location' in metadata:
            LOG.info(_("Found parent at %s"), metadata['parent_location'])
            parent_location = metadata['parent_location']
            parent_checksum = metadata['parent_checksum']
            self._incremental_restore(parent_location, parent_checksum)
            cmd = self._incremental_restore_cmd(incr=True)
            self.content_length += self._unpack(location, checksum, cmd)

        else:
            # For the parent base backup, revert to the default restore cmd
            LOG.info(_("Recursed back to full backup."))

            super(PgBaseBackupIncremental, self).pre_restore()
            cmd = self._incremental_restore_cmd(incr=False)
            self.content_length += self._unpack(location, checksum, cmd)

            operating_system.chmod(self.app.pgsql_data_dir,
                                   FileMode.SET_USR_RWX(),
                                   as_root=True, recursive=True, force=True)

    def _run_restore(self):
        self._incremental_restore(self.location, self.checksum)
        # content-length restored
        return self.content_length
