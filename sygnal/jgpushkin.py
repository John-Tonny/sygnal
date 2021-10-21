# -*- coding: utf-8 -*-
# Copyright 2014 Leon Handreke
# Copyright 2017 New Vector Ltd
# Copyright 2019-2020 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import logging
import time
from io import BytesIO

from opentracing import logs, tags
from prometheus_client import Counter, Gauge, Histogram
from twisted.internet.defer import DeferredSemaphore
from twisted.web.client import FileBodyProducer, HTTPConnectionPool, readBody
from twisted.web.http_headers import Headers

from sygnal.exceptions import (
    NotificationDispatchException,
    TemporaryNotificationDispatchException,
)
from sygnal.helper.context_factory import ClientTLSOptionsFactory
from sygnal.helper.proxy.proxyagent_twisted import ProxyAgent
from sygnal.utils import NotificationLoggerAdapter, json_decoder, twisted_sleep

from .exceptions import PushkinSetupException
from .notifications import ConcurrencyLimitedPushkin

import base64

QUEUE_TIME_HISTOGRAM = Histogram(
    "sygnal_jg_queue_time", "Time taken waiting for a connection to JGPUSH"
)

SEND_TIME_HISTOGRAM = Histogram(
    "sygnal_jg_request_time", "Time taken to send HTTP request to JGPUSH"
)

PENDING_REQUESTS_GAUGE = Gauge(
    "sygnal_pending_jg_requests", "Number of JGPUSH requests waiting for a connection"
)

ACTIVE_REQUESTS_GAUGE = Gauge(
    "sygnal_active_jg_requests", "Number of JGPUSH requests in flight"
)

RESPONSE_STATUS_CODES_COUNTER = Counter(
    "sygnal_jg_status_codes",
    "Number of HTTP response status codes received from JGPUSH",
    labelnames=["pushkin", "code"],
)

logger = logging.getLogger(__name__)

JGPUSH_URL = b"https://api.jpush.cn/v3/push"
MAX_TRIES = 3
RETRY_DELAY_BASE = 10
MAX_BYTES_PER_FIELD = 1024

# The error codes that mean a registration ID will never
# succeed and we should reject it upstream.
# We include NotRegistered here too for good measure, even
# though jg-client 'helpfully' extracts these into a separate
# list.
BAD_PUSHKEY_FAILURE_CODES = [
    "MissingRegistration",
    "InvalidRegistration",
    "NotRegistered",
    "InvalidPackageName",
    "MismatchSenderId",
]

# Failure codes that mean the message in question will never
# succeed, so don't retry, but the registration ID is fine
# so we should not reject it upstream.
BAD_MESSAGE_FAILURE_CODES = ["MessageTooBig", "InvalidDataKey", "InvalidTtl"]

DEFAULT_MAX_CONNECTIONS = 20


class JgPushkin(ConcurrencyLimitedPushkin):
    """
    Pushkin that relays notifications to Google/Firebase Cloud Messaging.
    """

    UNDERSTOOD_CONFIG_FIELDS = {
        "type",
        "api_key",
        "master_secret",        
        "jg_options",
        "max_connections",
    } | ConcurrencyLimitedPushkin.UNDERSTOOD_CONFIG_FIELDS

    def __init__(self, name, sygnal, config):
        super(JgPushkin, self).__init__(name, sygnal, config)

        nonunderstood = set(self.cfg.keys()).difference(self.UNDERSTOOD_CONFIG_FIELDS)
        if len(nonunderstood) > 0:
            logger.warning(
                "The following configuration fields are not understood: %s",
                nonunderstood,
            )

        self.http_pool = HTTPConnectionPool(reactor=sygnal.reactor)
        self.max_connections = self.get_config(
            "max_connections", DEFAULT_MAX_CONNECTIONS
        )
        self.connection_semaphore = DeferredSemaphore(self.max_connections)
        self.http_pool.maxPersistentPerHost = self.max_connections

        tls_client_options_factory = ClientTLSOptionsFactory()

        # use the Sygnal global proxy configuration
        proxy_url = sygnal.config.get("proxy")

        self.http_agent = ProxyAgent(
            reactor=sygnal.reactor,
            pool=self.http_pool,
            contextFactory=tls_client_options_factory,
            proxy_url_str=proxy_url,
        )

        self.api_key = self.get_config("api_key")
        if not self.api_key:
            raise PushkinSetupException("No API key set in config")
        self.master_secret = self.get_config("master_secret")
        if not self.master_secret:
            raise PushkinSetupException("No Master secret set in config")

        # Use the jg_options config dictionary as a foundation for the body;
        # this lets the Sygnal admin choose custom JGPUSH options
        # (e.g. content_available).
        self.base_request_body: dict = self.get_config("jg_options", {})
        if not isinstance(self.base_request_body, dict):
            raise PushkinSetupException(
                "Config field jg_options, if set, must be a dictionary of options"
            )

    @classmethod
    async def create(cls, name, sygnal, config):
        """
        Override this if your pushkin needs to call async code in order to
        be constructed. Otherwise, it defaults to just invoking the Python-standard
        __init__ constructor.

        Returns:
            an instance of this Pushkin
        """
        return cls(name, sygnal, config)

    async def _perform_http_request(self, body, headers):
        """
        Perform an HTTP request to the JGPUSH server with the body and headers
        specified.
        Args:
            body (nested dict): Body. Will be JSON-encoded.
            headers (Headers): HTTP Headers.

        Returns:

        """
        body_producer = FileBodyProducer(BytesIO(json.dumps(body).encode()))

        # we use the semaphore to actually limit the number of concurrent
        # requests, since the HTTPConnectionPool will actually just lead to more
        # requests being created but not pooled – it does not perform limiting.
        with QUEUE_TIME_HISTOGRAM.time():
            with PENDING_REQUESTS_GAUGE.track_inprogress():
                await self.connection_semaphore.acquire()

        try:
            with SEND_TIME_HISTOGRAM.time():
                with ACTIVE_REQUESTS_GAUGE.track_inprogress():
                    response = await self.http_agent.request(
                        b"POST",
                        JGPUSH_URL,
                        headers=Headers(headers),
                        bodyProducer=body_producer,
                    )
                    response_text = (await readBody(response)).decode()
        except Exception as exception:
            raise TemporaryNotificationDispatchException(
                "JGPUSH request failure"
            ) from exception
        finally:
            self.connection_semaphore.release()
        return response, response_text

    async def _request_dispatch(self, n, log, body, headers, pushkeys, span):
        poke_start_time = time.time()

        failed = []

        response, response_text = await self._perform_http_request(body, headers)

        RESPONSE_STATUS_CODES_COUNTER.labels(
            pushkin=self.name, code=response.code
        ).inc()

        log.debug("JGPUSH request took %f seconds", time.time() - poke_start_time)

        span.set_tag(tags.HTTP_STATUS_CODE, response.code)

        if 500 <= response.code < 600:
            log.debug("%d from server, waiting to try again", response.code)

            retry_after = None

            for header_value in response.headers.getRawHeaders(
                b"retry-after", default=[]
            ):
                retry_after = int(header_value)
                span.log_kv({"event": "jg_retry_after", "retry_after": retry_after})

            raise TemporaryNotificationDispatchException(
                "JGPUSH server error, hopefully temporary.", custom_retry_delay=retry_after
            )
        elif response.code == 400:
            log.error(
                "%d from server, we have sent something invalid! Error: %r",
                response.code,
                response_text,
            )
            # permanent failure: give up
            raise NotificationDispatchException("Invalid request")
        elif response.code == 401:
            log.error(
                "401 from server! Our API key is invalid? Error: %r", response_text
            )
            # permanent failure: give up
            raise NotificationDispatchException("Not authorised to push")
        elif response.code == 404:
            # assume they're all failed
            log.info("Reg IDs %r get 404 response; assuming unregistered", pushkeys)
            return pushkeys, []
        elif 200 <= response.code < 300:
            try:
                resp_object = json_decoder.decode(response_text)
            except ValueError:
                raise NotificationDispatchException("Invalid JSON response from JGPUSH.")
            if "msg_id" not in resp_object:
                log.error(
                    "%d from server but response contained no 'results' key: %r",
                    response.code,
                    response_text,
                )
            """
            if len(resp_object["results"]) < len(pushkeys):
                log.error(
                    "Sent %d notifications but only got %d responses!",
                    len(n.devices),
                    len(resp_object["results"]),
                )
                span.log_kv(
                    {
                        logs.EVENT: "jg_response_mismatch",
                        "num_devices": len(n.devices),
                        "num_results": len(resp_object["results"]),
                    }
                )

            # determine which pushkeys to retry or forget about
            new_pushkeys = []
            for i, result in enumerate(resp_object["results"]):
                if "error" in result:
                    log.warning(
                        "Error for pushkey %s: %s", pushkeys[i], result["error"]
                    )
                    span.set_tag("jg_error", result["error"])
                    if result["error"] in BAD_PUSHKEY_FAILURE_CODES:
                        log.info(
                            "Reg ID %r has permanently failed with code %r: "
                            "rejecting upstream",
                            pushkeys[i],
                            result["error"],
                        )
                        failed.append(pushkeys[i])
                    elif result["error"] in BAD_MESSAGE_FAILURE_CODES:
                        log.info(
                            "Message for reg ID %r has permanently failed with code %r",
                            pushkeys[i],
                            result["error"],
                        )
                    else:
                        log.info(
                            "Reg ID %r has temporarily failed with code %r",
                            pushkeys[i],
                            result["error"],
                        )
                        new_pushkeys.append(pushkeys[i])
            """
            new_pushkeys = []
            return failed, new_pushkeys
        else:
            raise NotificationDispatchException(
                f"Unknown JGPUSH response code {response.code}"
            )

    async def _dispatch_notification_unlimited(self, n, device, context):
        log = NotificationLoggerAdapter(logger, {"request_id": context.request_id})

        pushkeys = [
            device.pushkey for device in n.devices if device.app_id == self.name
        ]
        # Resolve canonical IDs for all pushkeys

        if pushkeys[0] != device.pushkey:
            # Only send notifications once, to all devices at once.
            return []

        # The pushkey is kind of secret because you can use it to send push
        # to someone.
        # span_tags = {"pushkeys": pushkeys}
        span_tags = {"jg_num_devices": len(pushkeys)}

        with self.sygnal.tracer.start_span(
            "jg_dispatch", tags=span_tags, child_of=context.opentracing_span
        ) as span_parent:
            data = JgPushkin._build_data(n, device)
            user_info_str = self.api_key + ":" + self.master_secret
            user_info = base64.b64encode(user_info_str.encode('utf-8'))
            headers = {
                b"User-Agent": ["sygnal"],
                b"Content-Type": ["application/json"],
                b"Authorization": ["Basic %s" % (user_info.decode())],
            }

            # TODO: Implement collapse_key to queue only one message per room.
            failed = []

            body = self.base_request_body.copy()

            #body["priority"] = "normal" if n.prio == "low" else "high"
            body["platform"] = "android"
            body["notification"] = {}
            # body["notification"]["alert"] = "Hi,JPush lht 202110071414!"
            body["notification"]["android"] = {}
            body["notification"]["android"]["alert"] = u"链聊通知"

            body["notification"]["android"]["extras"] = data

            body["audience"] = {}
            for retry_number in range(0, MAX_TRIES):
                body["audience"]["registration_id"] = pushkeys

                log.info("Sending (attempt %i) => %r", retry_number, pushkeys)
                
                try:
                    span_tags = {"retry_num": retry_number}

                    with self.sygnal.tracer.start_span(
                        "jg_dispatch_try", tags=span_tags, child_of=span_parent
                    ) as span:
                        new_failed, new_pushkeys = await self._request_dispatch(
                            n, log, body, headers, pushkeys, span
                        )
                    pushkeys = new_pushkeys
                    failed += new_failed

                    if len(pushkeys) == 0:
                        break
                except TemporaryNotificationDispatchException as exc:
                    retry_delay = RETRY_DELAY_BASE * (2 ** retry_number)
                    if exc.custom_retry_delay is not None:
                        retry_delay = exc.custom_retry_delay

                    log.warning(
                        "Temporary failure, will retry in %d seconds",
                        retry_delay,
                        exc_info=True,
                    )

                    span_parent.log_kv(
                        {"event": "temporary_fail", "retrying_in": retry_delay}
                    )

                    await twisted_sleep(
                        retry_delay, twisted_reactor=self.sygnal.reactor
                    )

            if len(pushkeys) > 0:
                log.info("Gave up retrying reg IDs: %r", pushkeys)
            # Count the number of failed devices.
            span_parent.set_tag("jg_num_failed", len(failed))
            return failed

    @staticmethod
    def _build_data(n, device):
        """
        Build the payload data to be sent.
        Args:
            n: Notification to build the payload for.
            device (Device): Device information to which the constructed payload
            will be sent.

        Returns:
            JSON-compatible dict
        """
        data = {}

        if device.data:
            data.update(device.data.get("default_payload", {}))

        for attr in [
            "event_id",
            "type",
            "sender",
            "room_name",
            "room_alias",
            "membership",
            "sender_display_name",
            "content",
            "room_id",
        ]:
            if hasattr(n, attr):
                data[attr] = getattr(n, attr)
                # Truncate fields to a sensible maximum length. If the whole
                # body is too long, JGPUSH will reject it.
                if data[attr] is not None and len(data[attr]) > MAX_BYTES_PER_FIELD:
                    data[attr] = data[attr][0:MAX_BYTES_PER_FIELD]

        data["prio"] = "high"
        if n.prio == "low":
            data["prio"] = "normal"

        if getattr(n, "counts", None):
            data["unread"] = n.counts.unread
            data["missed_calls"] = n.counts.missed_calls

        return data
