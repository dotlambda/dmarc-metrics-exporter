import asyncio
import smtplib
import time
from dataclasses import astuple, dataclass
from email.message import EmailMessage
from typing import Awaitable, Callable

import docker
import docker.models
import pytest

from dmarc_metrics_exporter.imap_queue import ConnectionConfig, ImapClient, ImapQueue


@dataclass
class NetworkAddress:
    host: str
    port: int


@dataclass
class Greenmail:
    smtp: NetworkAddress
    imap: ConnectionConfig


@pytest.fixture(name="docker_client")
def fixture_docker_client() -> docker.DockerClient:
    return docker.from_env()


@pytest.fixture(name="greenmail")
def fixture_greenmail(
    docker_client: docker.DockerClient,
) -> docker.models.containers.Container:
    container = docker_client.containers.run(
        "greenmail/standalone:1.6.0",
        detach=True,
        remove=True,
        ports={"3025/tcp": 3025, "3993/tcp": 3993},
    )
    yield Greenmail(
        smtp=NetworkAddress("localhost", 3025),
        imap=ConnectionConfig(
            host="localhost", port=3993, username="queue@localhost", password="password"
        ),
    )
    container.stop()


async def try_until_success(
    function: Callable[[], Awaitable],
    timeout_seconds: int = 10,
    max_fn_duration_seconds: int = 1,
    poll_interval_seconds: float = 0.1,
):
    timeout = time.time() + timeout_seconds
    last_err = None
    while time.time() < timeout:
        try:
            await asyncio.wait_for(function(), max_fn_duration_seconds)
            return
        except Exception as err:  # pylint: disable=broad-except
            last_err = err
            await asyncio.sleep(poll_interval_seconds)
    raise TimeoutError(
        f"Call to {function} not successful within {timeout_seconds} seconds."
    ) from last_err


async def send_email(msg: EmailMessage, network_address: NetworkAddress):
    smtp = smtplib.SMTP(*astuple(network_address))
    smtp.send_message(msg)
    smtp.quit()


async def verify_email_delivered(connection: ConnectionConfig, mailboxes=("INBOX",)):
    async with ImapClient(connection) as client:
        msg_counts = await asyncio.gather(
            *(client.select(mailbox) for mailbox in mailboxes)
        )
        assert any(count > 0 for count in msg_counts)


async def verify_imap_available(connection: ConnectionConfig):
    async with ImapClient(connection):
        pass


def create_dummy_email(to: str):
    msg = EmailMessage()
    msg.set_content("message content")
    msg["Subject"] = "Message subject"
    msg["From"] = "sender@some-domain.org"
    msg["To"] = to
    return msg


def assert_emails_equal(a: EmailMessage, b: EmailMessage):
    assert all(a[header] == b[header] for header in ("Subject", "From", "To"))
    assert a.get_content().strip() == b.get_content().strip()


@pytest.mark.asyncio
async def test_successful_processing_of_existing_queue_message(greenmail):
    # Given
    msg = create_dummy_email(greenmail.imap.username)
    await try_until_success(lambda: send_email(msg, greenmail.smtp))
    await try_until_success(lambda: verify_email_delivered(greenmail.imap))

    is_done = asyncio.Event()

    async def handler(queue_msg: EmailMessage, is_done=is_done):
        is_done.set()
        assert_emails_equal(queue_msg, msg)

    # When
    queue = ImapQueue(connection=greenmail.imap)
    queue.consume(handler)
    try:
        await asyncio.wait_for(is_done.wait(), 10)
    finally:
        await queue.stop_consumer()

    # Then
    async with ImapClient(greenmail.imap) as client:
        assert await client.select() == 0
        assert await client.select(queue.folders.done) == 1


@pytest.mark.asyncio
async def test_successful_processing_of_incoming_queue_message(greenmail):
    # Given
    msg = create_dummy_email(greenmail.imap.username)

    is_done = asyncio.Event()

    async def handler(queue_msg: EmailMessage, is_done=is_done):
        is_done.set()
        assert_emails_equal(queue_msg, msg)

    # When
    await try_until_success(lambda: verify_imap_available(greenmail.imap))
    queue = ImapQueue(connection=greenmail.imap, poll_interval_seconds=0.1)
    queue.consume(handler)

    await asyncio.sleep(0.5)
    await try_until_success(lambda: send_email(msg, greenmail.smtp))
    await try_until_success(
        lambda: verify_email_delivered(
            greenmail.imap, mailboxes=("INBOX", queue.folders.done)
        )
    )

    try:
        await asyncio.wait_for(is_done.wait(), 10)
    finally:
        await queue.stop_consumer()

    # Then
    async with ImapClient(greenmail.imap) as client:
        assert await client.select() == 0
        assert await client.select(queue.folders.done) == 1


@pytest.mark.asyncio
async def test_error_handling_when_processing_queue_message(greenmail):
    # Given
    msg = create_dummy_email(greenmail.imap.username)
    await try_until_success(lambda: send_email(msg, greenmail.smtp))
    await try_until_success(lambda: verify_email_delivered(greenmail.imap))

    is_done = asyncio.Event()

    async def handler(_queue_msg: EmailMessage, is_done=is_done):
        is_done.set()
        raise Exception("Error raised on purpose.")

    # When
    queue = ImapQueue(connection=greenmail.imap)
    queue.consume(handler)
    try:
        await asyncio.wait_for(is_done.wait(), 10)
    finally:
        await queue.stop_consumer()

    # Then
    async with ImapClient(greenmail.imap) as client:
        assert await client.select() == 0
        assert await client.select(queue.folders.error) == 1
