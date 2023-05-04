import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import aio_pika
import logging
import json
from aio_pika.pool import Pool
from instance_manager.instance.instance_service import InstanceService
from instance_manager.message import Message, Action
from instance_manager.message.del_device_message import AckDeleteMessage
from instance_manager.message.message_handler import message_handler
from src.instance_manager.message.ack_status_message import AckStatusMessage

logger = logging.getLogger(__name__)


async def start_rabbitmq_consumer_pool(
        consumer_info, instance_service: InstanceService, event_loop
) -> None:
    logger.info("Starting RabbitMQ consumer...")
    logger.debug(f"Connection info: {consumer_info}")

    rbt_user = consumer_info["user"]
    rbt_pass = consumer_info["password"]
    rbt_host = consumer_info["host"]
    rbt_port = consumer_info["port"]
    rbt_controller_queue_name = consumer_info["controller_queue"]
    rbt_ack_status_queue_name = consumer_info["ack_status_queue"]
    rbt_ack_delete_queue_name = consumer_info["ack_delete_queue"]

    rabbit_url = f"amqp://{rbt_user}:{rbt_pass}@{rbt_host}:{rbt_port}/"
    logger.debug(f"RabbitMQ Built URL: {rabbit_url}")

    rabbit_manager = RabbitMQManager(rabbit_url, event_loop, instance_service)
    rabbit_client = RabbitMQClient(rabbit_manager, instance_service)

    await rabbit_client.start_consumer(
        rbt_controller_queue_name,
        rbt_ack_status_queue_name,
        rbt_ack_delete_queue_name
    )


class RabbitMQManager:
    def __init__(self, rabbit_url, event_loop, instance_service):
        self.connection_pool = Pool(
            self.get_connection, max_size=2, loop=event_loop
        )
        self.channel_pool = Pool(
            self.get_channel, max_size=10, loop=event_loop
        )
        self.rabbit_url = rabbit_url
        self.event_loop = event_loop
        self.instance_service = instance_service

    async def get_connection(self):
        return await aio_pika.connect_robust(self.rabbit_url)

    async def get_channel(self) -> aio_pika.Channel:
        async with self.connection_pool.acquire() as connection:
            return await connection.channel()


class RabbitMQClient:
    def __init__(self, manager, instance_service):
        self.manager = manager
        self.instance_service = instance_service
        self.consumer_pool = ThreadPoolExecutor(max_workers=5)
        self.loop = asyncio.get_event_loop()

    async def consume(self, ctl_queue_name, ack_status_queue_name, ack_delete_queue_name) -> None:
        async with self.manager.channel_pool.acquire() as channel:
            await channel.set_qos(10)  # quality of service for the channel

            queue = await channel.declare_queue(
                ctl_queue_name, durable=True, auto_delete=False,
            )

            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    async with message.process():
                        self.loop.run_in_executor(
                            self.consumer_pool,
                            functools.partial(
                                self.loop.create_task,
                                self.process_message(
                                    ack_status_queue_name, ack_delete_queue_name, message
                                )
                            )
                        )

    async def start_consumer(self, ctl_queue_name, ack_status_queue_name, ack_delete_queue_name):
        async with self.manager.connection_pool, self.manager.channel_pool:
            task = self.manager.event_loop.create_task(
                self.consume(ctl_queue_name, ack_status_queue_name, ack_delete_queue_name)
            )
            await task
            
    async def send_message(self, queue_name, message):
        logger.info(f"Sending message {message.to_dict()} to {queue_name}...")
        async with self.manager.channel_pool.acquire() as channel:
            exchange = await channel.declare_exchange(
                "queue_exchange", durable=True
            )

            ack_queue = await channel.declare_queue(
                queue_name, durable=True
            )

            await ack_queue.bind(exchange, queue_name)

            await exchange.publish(
                aio_pika.Message(
                    body=json.dumps(message.to_dict()).encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                ),
                queue_name
            )

    async def process_message(
            self,
            status_queue_name,
            delete_queue_name,
            message
    ):
        """
         Callback function for processing received messages from RabbitMQ.
        """
        logger.info("Starting to process message...")
        message_body = message.body.decode() 
        message_dict = json.loads(message_body)  # TODO: fix when message is not json

        message_dto = Message(
            action=Action[message_dict["action"]],
            device_id=message_dict["device_id"],
            device_stream_url=message_dict.get("device_stream_url")
        )

        logger.info(f"Received message: {message_dto}")

        try:
            await message_handler(message_dto, self.instance_service)

            # TODO: send 4004 if error when the container to remove is not found
            if message_dto.action == Action.REMOVE:
                queue_name = delete_queue_name
                message = AckDeleteMessage(
                    device_id=message_dto.device_id,
                )

            else:
                queue_name = status_queue_name
                message = AckStatusMessage(
                    code=2000,
                    device_id=message_dto.device_id,
                    state=message_dict["action"],
                    message="OK"
                )

            await self.send_message(queue_name, message)

        except Exception as e:
            logger.error(f"Error while handling message: {e}")
            message = AckStatusMessage(
                code=4000,
                device_id=message_dto.device_id,
                state=message_dict["action"],
                message=f"{e}"
            )
            await self.send_message(status_queue_name, message)
        return
