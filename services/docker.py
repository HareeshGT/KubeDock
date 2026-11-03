"""Docker domain operations backed by the shared command service."""

from __future__ import annotations

from .lifecycle import activate_lifecycle


class DockerService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    def create_worker(
        self, worker_pool, command, *, sudo_user=None, timeout=None,
        raw=False, operation_key=None, diagnostic_key="docker:command",
    ):
        activate_lifecycle(self)
        return self.commands.create_worker(
            worker_pool, command, sudo_user=sudo_user, timeout=timeout,
            raw=raw, operation_key=operation_key, diagnostic_key=diagnostic_key,
        )

    # Docker CLI command policy belongs to this domain service. The UI owns
    # confirmation dialogs and rendering, while command construction and
    # validation remain reusable and independently testable.
    @staticmethod
    def list_command(kind):
        import docker_utils
        return docker_utils.list_command(kind)

    @staticmethod
    def stats_command():
        import docker_utils
        return docker_utils.stats_command()

    @staticmethod
    def details_command(kind):
        import docker_utils
        return docker_utils.details_command(kind)

    @staticmethod
    def container_action_command(action, name):
        import docker_utils
        return docker_utils.container_action_command(action, name)

    @staticmethod
    def exec_command(name, command):
        import docker_utils
        return docker_utils.exec_command(name, command)

    @staticmethod
    def inspect_command(kind, ident):
        import docker_utils
        return docker_utils.inspect_command(kind, ident)

    @staticmethod
    def remove_command(kind, ident, *, force=False):
        import docker_utils
        builders = {
            "containers": lambda: docker_utils.remove_container_command(ident, force=force),
            "images": lambda: docker_utils.remove_image_command(ident),
            "volumes": lambda: docker_utils.remove_volume_command(ident),
            "networks": lambda: docker_utils.remove_network_command(ident),
        }
        try:
            return builders[kind]()
        except KeyError:
            raise ValueError("Unsupported Docker resource type: {}".format(kind)) from None

    @staticmethod
    def pull_command(image):
        import docker_utils
        return docker_utils.pull_command(image)

    def create_log_stream_worker(self, command):
        """Create a live Docker log worker bound to the active session generation."""
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before streaming Docker logs")
        from workers import ExecStreamWorker

        worker = ExecStreamWorker(ssh, command)
        self.connection.bind_worker(worker, snapshot)
        return self.commands.register_worker(worker, diagnostic_key="docker:logs")
