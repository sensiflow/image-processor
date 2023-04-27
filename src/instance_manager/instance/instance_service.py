from database.transaction import transaction
from instance_manager.exception import InstanceNotFound
from instance_manager.instance.instance import Instance
from instance_manager.instance.instance_dao import InstanceDAOFactory
from docker_manager.docker_api import DockerApi
from psycopg_pool import ConnectionPool
import logging


logger = logging.getLogger(__name__)


class InstanceService:
    def __init__(
                    self,
                    conn_manager: ConnectionPool,
                    dao_factory: InstanceDAOFactory,
                    docker_api: DockerApi
                ):
        self.conn_manager = conn_manager
        self.dao_factory = dao_factory
        self.docker_api = docker_api

    async def get_instance(self, instance_id: str) -> Instance:
        with transaction(self.conn_manager) as cursor:
            instance_dao = self.dao_factory.create_dao(cursor)
            instance = instance_dao.get_instance(instance_id)
            if instance is None:
                raise InstanceNotFound(instance_id)

            return instance

    async def create_instance(
            self,
            instance: Instance,
            stream_url: str
            ) -> str:
        with transaction(self.conn_manager) as cursor:
            instance_dao = self.dao_factory.create_dao(cursor)
            instance_id = instance_dao.create_instance(instance)
            logger.info(f"Created instance {instance_id}")
            await self.docker_api.run_container(instance.id,
                                                "--source",
                                                stream_url)
            return instance_id

    async def update_instance(self, instance) -> bool:
        with transaction(self.conn_manager) as cursor:
            instance_dao = self.dao_factory.create_dao(cursor)
            updated_rows = instance_dao.update_instance(instance)

            if updated_rows == 0:
                raise InstanceNotFound(instance.id)
            return updated_rows == 1

    async def delete_instance(self, instance_id) -> bool:
        with transaction(self.conn_manager) as cursor:
            instance_dao = self.dao_factory.create_dao(cursor)
            deleted_rows = instance_dao.delete_instance(instance_id)

            if deleted_rows == 0:
                raise InstanceNotFound(instance_id)

            return deleted_rows == 1
