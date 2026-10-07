"""workers.py — Background QThread workers for SSH commands and file transfers.

Compatibility facade: implementation classes/helpers live in ``workers_parts``.
All historical module-level symbols are re-exported here so existing imports
continue to work unchanged.
"""

from workers_parts.ssh import (
    _SSH_POOL_MAX_CONNECTIONS,
    _SSH_MAX_CHANNELS_PER_CONNECTION,
    _SSH_GUARD_ATTR,
    _SSH_POOL_ATTR,
    _DEFAULT_SESSION_WAIT,
    _configure_host_key_policy,
    _configure_transport,
    _SSHPoolSlot,
    SSHConnectionPool,
    attach_ssh_connection_pool,
    close_ssh_connection_pool,
    _ssh_guard,
    open_managed_session,
    close_managed_session,
    managed_exec_command,
    track_worker,
)
from workers_parts.directory import DirectoryListWorker
from workers_parts.connection import ConnectWorker, FTPConnectionWorker, EFSConnectionWorker, ConnectionHealthWorker
from workers_parts.command import CommandWorker, PodExecStreamWorker
from workers_parts.transfer import FileStreamReadWorker, _TransferWorker, _PtyProc, ScpTransferWorker
from workers_parts.media import (
    _ChannelReader,
    _SFTPStreamReader,
    _RangeHTTPRequestHandler,
    _MediaStreamHTTPServer,
    MediaStreamServer,
    AudioTranscodeWorker,
    VideoTranscodeWorker,
    _StreamServerStartWorker,
)

__all__ = [
    "DirectoryListWorker",
    "ConnectWorker",
    "FTPConnectionWorker",
    "EFSConnectionWorker",
    "ConnectionHealthWorker",
    "CommandWorker",
    "PodExecStreamWorker",
    "FileStreamReadWorker",
    "_TransferWorker",
    "_PtyProc",
    "ScpTransferWorker",
    "_ChannelReader",
    "_SFTPStreamReader",
    "_RangeHTTPRequestHandler",
    "_MediaStreamHTTPServer",
    "MediaStreamServer",
    "AudioTranscodeWorker",
    "VideoTranscodeWorker",
    "_StreamServerStartWorker",
    "SSHConnectionPool",
    "attach_ssh_connection_pool",
    "close_ssh_connection_pool",
    "open_managed_session",
    "close_managed_session",
    "managed_exec_command",
    "track_worker",
]
