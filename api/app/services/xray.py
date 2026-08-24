import sys
import os
from pathlib import Path

import grpc

XRAY_PROTO_PATH = str(Path(__file__).resolve().parents[1] / "xray")
if XRAY_PROTO_PATH not in sys.path:
    sys.path.insert(0, XRAY_PROTO_PATH)

from app.proxyman.command import command_pb2
from app.proxyman.command import command_pb2_grpc
from common.protocol import user_pb2
from common.serial import typed_message_pb2
from proxy.vless import account_pb2


class XrayError(Exception):
    pass


class XrayUserNotFound(XrayError):
    pass


class XrayUserAlreadyExists(XrayError):
    pass


class XrayClient:
    def __init__(
        self,
        address: str | None = None,
        timeout: float = 3.0,
    ):
        self.address = address or os.getenv("XRAY_API_ADDRESS") or "172.18.0.1:10085"
        self.timeout = timeout

    def _stub(self):
        channel = grpc.aio.insecure_channel(self.address)
        return channel, command_pb2_grpc.HandlerServiceStub(channel)

    async def add_vless_user(
        self,
        inbound_tag: str,
        client_uuid: str,
        email: str,
        level: int = 0,
        flow: str = "",
    ) -> None:
        account = account_pb2.Account(
            id=client_uuid,
            flow=flow,
            encryption="none",
        )

        account_message = typed_message_pb2.TypedMessage(
            type="xray.proxy.vless.Account",
            value=account.SerializeToString(),
        )

        user = user_pb2.User(
            level=level,
            email=email,
            account=account_message,
        )

        operation = command_pb2.AddUserOperation(
            user=user,
        )

        operation_message = typed_message_pb2.TypedMessage(
            type="xray.app.proxyman.command.AddUserOperation",
            value=operation.SerializeToString(),
        )

        request = command_pb2.AlterInboundRequest(
            tag=inbound_tag,
            operation=operation_message,
        )

        channel, stub = self._stub()

        try:
            await stub.AlterInbound(
                request,
                timeout=self.timeout,
            )

        except grpc.aio.AioRpcError as exc:
            details = exc.details() or ""
            if "already exists" in details.lower():
                raise XrayUserAlreadyExists(
                    f"Xray user {email} already exists"
                ) from exc
            raise XrayError(
                f"Failed to add VLESS user to Xray: {exc}"
            ) from exc

        finally:
            await channel.close()

    async def remove_vless_user(
        self,
        inbound_tag: str,
        email: str,
    ) -> None:
        operation = command_pb2.RemoveUserOperation(
            email=email,
        )

        operation_message = typed_message_pb2.TypedMessage(
            type="xray.app.proxyman.command.RemoveUserOperation",
            value=operation.SerializeToString(),
        )

        request = command_pb2.AlterInboundRequest(
            tag=inbound_tag,
            operation=operation_message,
        )

        channel, stub = self._stub()

        try:
            await stub.AlterInbound(
                request,
                timeout=self.timeout,
            )

        except grpc.aio.AioRpcError as exc:
            details = exc.details() or ""
            lowered = details.lower()

            if (
                exc.code() == grpc.StatusCode.UNKNOWN
                and "user" in lowered
                and "not found" in lowered
            ):
                raise XrayUserNotFound(
                    f"Xray user {email} is already absent"
                ) from exc

            raise XrayError(
                f"Failed to remove VLESS user from Xray: {exc}"
            ) from exc

        finally:
            await channel.close()

    async def get_users(
        self,
        inbound_tag: str,
    ):
        channel, stub = self._stub()

        try:
            response = await stub.GetInboundUsers(
                command_pb2.GetInboundUserRequest(
                    tag=inbound_tag,
                ),
                timeout=self.timeout,
            )

            return list(response.users)

        except grpc.aio.AioRpcError as exc:
            raise XrayError(
                f"Failed to get Xray users: {exc}"
            ) from exc

        finally:
            await channel.close()
