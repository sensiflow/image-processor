import pytest
from datetime import datetime, timedelta
from psycopg_pool import ConnectionPool
import psycopg

from config import (
    parse_config,
    get_environment_type,
    DATABASE_SECTION,
    DATABASE_URL_KEY,
)
from image_processor.processed_stream.processed_stream_dao import ProcessedStreamDAOFactory
from instance_manager.instance.instance_service import InstanceService
from instance_manager.instance.instance import Instance, InstanceStatus
from instance_manager.instance.instance_dao import InstanceDAOFactory
from instance_manager.exception import InstanceNotFound, DomainLogicError


@pytest.fixture(scope="session")
def db_url():
    config = parse_config(get_environment_type())
    return config[DATABASE_SECTION][DATABASE_URL_KEY]


@pytest.fixture(scope="session")
def manage_table(db_url):
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                create table if not exists instance(
                    id varchar(50) UNIQUE NOT null,
                    status VARCHAR(255)
                    NOT NULL
                    CHECK (status IN ('ACTIVE', 'INACTIVE', 'PAUSED')),
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                    CONSTRAINT updated_at_check
                    CHECK (updated_at >= created_at)
                );
            """
            )
    yield
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DROP TABLE instance")


@pytest.fixture(scope="session")
def service(db_url):
    return InstanceService(
        ConnectionPool(db_url),
        InstanceDAOFactory(),
        ProcessedStreamDAOFactory(),
        # DockerApiMock(), TODO: Mock DockerApi
    )


def test_create_instance(service, manage_table):
    created_at = datetime.utcnow()
    instance = Instance(
        id="instance_device_2",
        status=InstanceStatus.ACTIVE,
        created_at=created_at,
        updated_at=created_at,
    )
    instance_id = service.create_instance(instance)

    instance = service.get_instance(instance_id)
    expected_instance = Instance(
        id=instance_id,
        status=InstanceStatus.ACTIVE,
        created_at=created_at,
        updated_at=created_at,
    )

    assert instance == expected_instance


def test_get_instance(service, manage_table):
    instance = Instance(
        id="instance_device_3",
        status=InstanceStatus.ACTIVE,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    instance_id = service.create_instance(instance)
    retrieved_instance = service.get_instance(instance_id)
    expected_instance = Instance(
        id=instance_id,
        status=InstanceStatus.ACTIVE,
        created_at=instance.created_at,
        updated_at=instance.updated_at,
    )

    assert retrieved_instance == expected_instance


def test_get_non_existent_instance(service, manage_table):
    with pytest.raises(InstanceNotFound):
        service.get_instance("nonexistant")


def test_update_instance(service, manage_table):
    instance = Instance(
        id="instance_device_4",
        status=InstanceStatus.ACTIVE,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    instance_id = service.create_instance(instance)
    new_instance = Instance(
        id=instance_id,
        status=InstanceStatus.INACTIVE,
        created_at=instance.created_at,
        updated_at=datetime.utcnow(),
    )
    service.update_instance(new_instance)
    retrieved_instance = service.get_instance(instance_id)
    assert new_instance == retrieved_instance


def test_update_non_existent_instance(service, manage_table):
    instance = Instance(
        id="nonexistant",
        status=InstanceStatus.ACTIVE,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    with pytest.raises(InstanceNotFound):
        service.update_instance(instance)


def test_update_instance_with_updated_at_before_created_at(
        service, manage_table
):
    instance = Instance(
        id="instance_device_5",
        status=InstanceStatus.ACTIVE,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    instance_id = service.create_instance(instance)

    with pytest.raises(DomainLogicError):
        new_instance = Instance(
            id=instance_id,
            status=InstanceStatus.ACTIVE,
            created_at=instance.created_at,
            updated_at=instance.created_at - timedelta(days=1),
        )
        service.update_instance(new_instance)


def test_delete_instance(service, manage_table):
    instance = Instance(
        id="instance_device_6",
        status=InstanceStatus.ACTIVE,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    instance_id = service.create_instance(instance)
    service.delete_instance(instance_id)
    with pytest.raises(InstanceNotFound):
        service.get_instance(instance_id)
