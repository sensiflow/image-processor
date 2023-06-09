import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
from datetime import datetime

import docker
import logging
from docker.errors import APIError, DockerException, NotFound
from docker import types
from src.docker_manager.exceptions import (
    ContainerExitedError,
    ContainerGoalTimeout,
    ContainerNotFound
)
from docker_manager.docker_init import ProcessingMode

logger = logging.getLogger(__name__)


class DockerApi:
    """
        Manage docker engine
    """
    restart_policy = {"Name": "on-failure", "MaximumRetryCount": 1}
    network_mode = "host"
    # TODO: mudar para uma constante o nome do ficheiro
    entrypoint = ["poetry", "run", "python",
                  "transmit.py", "--weights", "yolov5s.pt", "--class" ,"0"]
    #TODO: to easily add support for other types of detection it is possible to change the class parameter

    def __init__(self, processor_image: str,
                 processing_mode: ProcessingMode):
        self.client = docker.from_env()
        self.processor_image = processor_image
        self.api_pool = ThreadPoolExecutor(max_workers=5)
        self.loop = asyncio.get_event_loop()
        self.processing_mode = processing_mode
        self.device_requests = self._get_device_requests()
        self.ipc_mode = "host"
        self.environment = {"ENVIRONMENT": "worker"}

    def _get_device_requests(self):
        if ProcessingMode[self.processing_mode.name] == ProcessingMode.CPU:
            return []
        if ProcessingMode[self.processing_mode.name] == ProcessingMode.GPU:
            # cont -1 is ALL GPUs
            return [types.DeviceRequest(count=-1, capabilities=[["gpu"]])]

    async def check_health(self):
        """
        Checks if docker engine is running
        Throws:
            DockerException: Error while fetching server API version
            APIError: If the server returns an error.
        """
        try:
            await self.loop.run_in_executor(
                self.api_pool,
                self.client.ping
            )
        except (DockerException, APIError) as e:
            logger.error("Docker server not responsive")
            raise e

    async def get_container(self, container_name):
        """
        Gets the container with the given name
        Parameters:
            container_name: the name of the container to get
        Returns:
            the container with the given name
        Throws:
            ContainerNotFound - if the container with the given name
                                does not exist
            DockerException: Error while fetching server API version
            APIError: If the server returns an error.
        """
        try:
            logger.info(f"Getting container {container_name}")
            return await self.loop.run_in_executor(
                self.api_pool,
                self.client.containers.get,
                container_name
            )
        except NotFound:
            logger.error(f"Container {container_name} not found")
            raise ContainerNotFound(container_name)
        except (DockerException, APIError) as e:
            logger.error(f"Error getting container {container_name}: {e}")
            raise e

    async def stop_container(
        self,
        container_name: str,
        timeout=5
    ):
        """
        Stops the container with the given name
        Parameters:
            container_name: the name of the container to stop
            timeout: the time to wait for the container to stop
        Throws:
            ContainerNotFound - if the container with the given name
                                does not exist
            DockerException: Error while fetching server API version
            APIError: If the server returns an error.
        """
        try:
            container = await self.get_container(container_name)
            await self.loop.run_in_executor(
                self.api_pool,
                functools.partial(
                    container.stop,
                    timeout=timeout
                )
            )
        except (DockerException, APIError) as e:
            logger.error(f"Error stopping container {container_name}")
            raise e

    async def remove_container(
            self,
            container_name: str,
            force=False,
            timeout=5
    ):
        """
        Removes the container with the given name
        Parameters:
            container_name: the name of the container to remove
            force: if true, the container is killed and removed immediately
                    otherwise the method waits for the container to stop
                    and then removes it
            timeout: the time to wait for the container to stop,
                        when this timeout is reached a SIGKILL is sent to the
                        container
        Throws:
            ContainerNotFound - if the container with the given name
                                does not exist
            DockerException: Error while fetching server API version
            APIError: If the server returns an error.
        """
        try:
            container = await self.get_container(container_name)
            await self.loop.run_in_executor(
                self.api_pool,
                self.__wait_container_removal,
                container, container_name, force, timeout
            )
        except (DockerException, APIError) as e:
            logger.error(f"Error removing container {container_name}")
            raise e

    def __wait_container_removal(
            self,
            container,
            container_name,
            force,
            timeout
    ):
        """
        Removes the container with the given name
        Parameters:
            container: the container to remove
            container_name: the name of the container to remove
            force: if true, the container is killed and removed immediately
                otherwise the method waits for the container to stop
                and then removes it
            timeout: the time to wait for the container to stop,
                    when this timeout is reached a SIGKILL is sent to the
                    container
        """
        if container.status != "exited":
            logger.info(f"Stopping container {container_name}")
            container.stop(timeout=timeout)

        if force:
            logger.info(f"Forcefully Removing container {container_name}")
            container.remove(force=True)
        else:
            container.wait()
            logger.info(f"Removing container {container_name}")
            container.remove()

    async def run_container(self, container_name: str, *extra_args: str):
        """
        Runs the container with the given name
        Parameters:
            container_name: the name of the container to run
            args: the arguments to pass to the container.
        Throws:
            DockerException: Error while fetching server API version.
            APIError: If the server returns an error.
        """
        logger.info(f"Creating container {container_name}")
        # run dockerfile with name
        try:
            if ProcessingMode[self.processing_mode.name] == ProcessingMode.CPU:
                args = ["--device", "cpu"]
            else:
                args = ["--device", "0"]

            docker_args = args + list(extra_args)

            container = await self.loop.run_in_executor(
                self.api_pool,
                self.__run_container,
                container_name,
                *docker_args,
            )

            await self.__wait_goals(container)

        except (DockerException, APIError) as e:
            logger.error("Error starting container")
            raise e

    def __run_container(self, container_name: str, *args):
        # Add ENVIRONMENT= to args
        return self.client.containers.run(
            name=container_name,
            image=self.processor_image,
            detach=True,
            restart_policy=self.restart_policy,
            network_mode=self.network_mode,
            entrypoint=self.entrypoint,
            device_requests=self.device_requests,
            ipc_mode=self.ipc_mode,
            command=args,
            environment=self.environment
        )

    async def __wait_goals(self, container, timeout_seconds=60):
        """
         Scans container logs for goal messages
         returns when the final goal is reached

         Parameters:
            container: the name of the container to scan
            timeout_seconds: the maximum time to wait for the final goal

         Throws:
            GoalTimeout: if the timeout is reached
            before the final goal is reached
        """
        logger.info(f"Waiting for goals in container {container.name}")

        def goal_reached(container):

            for line in container.logs(stream=True, follow=True, tail=5): # We only care about the last 10 lines
                decodedLine = line.decode("utf-8")
                logger.info(decodedLine)

                if b"[ERROR" in line:
                    error = decodedLine.split("]")[1]
                    logger.error(error)
                    return (False,error)

                if b"[SUCCESS 4]" in line:
                    logger.info("Started Streaming")
                    return (True, "")

            return (False, "Did not reach final goal")

        task = self.loop.run_in_executor(
            self.api_pool,
            goal_reached,
            container
        )
        try:
            (success,error) = await asyncio.wait_for(task, timeout_seconds)
            if not success:
                logger.error(f"Container {container.name} failed to start")
                await self.remove_container(container.name, force=True)
                raise ContainerExitedError(container.name,error)
        except (asyncio.TimeoutError):
            await self.remove_container(container.name, force=True)
            raise ContainerGoalTimeout(container.name)

    async def pause_container(self, container_name: str):
        """
        Pauses the container with the given name
        Parameters:
            container_name: the name of the container to pause
        Throws:
            ContainerNotFound - if the container with the given name
                                does not exist
            DockerException: Error while fetching server API version
            APIError: If the server returns an error.
        """
        try:
            container = await self.get_container(container_name)
            await self.loop.run_in_executor(
                self.api_pool,
                container.pause
            )
        except (DockerException, APIError) as e:
            logger.error(f"Error pausing container {container_name}")
            raise e

    async def unpause_container(self, container_name: str):
        """
        Unpauses the container with the given name
        Parameters:
            container_name: the name of the container to unpause
        Throws:
            ContainerNotFound - if the container with the given name
                                does not exist
            DockerException: Error while fetching server API version
            APIError: If the server returns an error.
        """
        try:
            container = await self.get_container(container_name)
            await self.loop.run_in_executor(
                self.api_pool,
                container.unpause
            )
        except (DockerException, APIError) as e:
            logger.error(f"Error unpausing container {container_name}")
            raise e

    async def start_container(self, container_name: str):
        """
        Starts the container with the given name
        Parameters:
            container_name: the name of the container to start
        Throws:
            ContainerNotFound - if the container with the given name
                                does not exist
            DockerException: Error while fetching server API version
            APIError: If the server returns an error.
        """
        try:
            container = await self.get_container(container_name)
            await self.loop.run_in_executor(
                self.api_pool,
                container.start
            )

            await self.__wait_goals(container)
        except (DockerException, APIError) as e:
            logger.error(f"Error starting container {container_name}")
            raise e
