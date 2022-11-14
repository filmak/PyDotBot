"""Interface of the Dotbot controller."""

import asyncio
import json
import os
import pickle
import time
import webbrowser

from abc import ABC, abstractmethod
from binascii import hexlify
from dataclasses import dataclass
from typing import Dict

import serial

import websockets
from fastapi import WebSocket

from rich.live import Live
from rich.table import Table

from bot_controller.hdlc import HDLCHandler, HDLCState, hdlc_encode
from bot_controller.protocol import (
    ProtocolPayload,
    ProtocolHeader,
    PROTOCOL_VERSION,
    ProtocolPayloadParserException,
    PayloadType,
)
from bot_controller.serial_interface import SerialInterface, SerialInterfaceException

# from bot_controller.models import DotBotModel, DotBotLH2Position, DotBotRgbLedCommandModel
from bot_controller.models import DotBotModel
from bot_controller.server import web
from bot_controller.lighthouse2 import save_camera_points, compute_coordinates

CONTROLLERS = {}
DEFAULT_CALIBRATION_DIR = "/tmp"


class ControllerException(Exception):
    """Exception raised by Dotbot controllers."""


@dataclass
class ControllerSettings:  # pylint: disable=too-many-instance-attributes
    """Data class that holds controller settings."""

    port: str
    baudrate: int
    dotbot_address: str
    gw_address: str
    swarm_id: str
    calibration_dir: str = DEFAULT_CALIBRATION_DIR
    calibrate: bool = False
    webbrowser: bool = False
    verbose: bool = False


class ControllerBase(ABC):
    """Abstract base class of specific implementations of Dotbot controllers."""

    def __init__(self, settings: ControllerSettings):
        # self.dotbots: Dict[str, DotBotModel] = {
        #     "0000000000000001": DotBotModel(
        #         address="0000000000000001",
        #         last_seen=time.time(),
        #         lh2_position=DotBotLH2Position(x=50, y=25, z=0),
        #         rgb_led=DotBotRgbLedCommandModel(red=255, green=0, blue=0),
        #     ),
        #     "0000000000000002": DotBotModel(
        #         address="0000000000000002",
        #         last_seen=time.time(),
        #         lh2_position=DotBotLH2Position(x=25, y=50, z=0),
        #         rgb_led=DotBotRgbLedCommandModel(red=0, green=255, blue=0),
        #     ),
        #     "0000000000000003": DotBotModel(
        #         address="0000000000000003",
        #         last_seen=time.time(),
        #     ),
        # }
        self.dotbots: Dict[str, DotBotModel] = {}
        self.header = ProtocolHeader(
            int(settings.dotbot_address, 16),
            int(settings.gw_address, 16),
            int(settings.swarm_id, 16),
            PROTOCOL_VERSION,
        )
        self.settings = settings
        self.hdlc_handler = HDLCHandler()
        self.serial = None
        self.websockets = []
        calibration_path = os.path.join(
            self.settings.calibration_dir, "/tmp/calibration.out"
        )
        if os.path.exists(calibration_path):
            with open(calibration_path, "rb") as calibration_file:
                self.calibration = pickle.load(calibration_file)
        else:
            self.calibration = None

    @abstractmethod
    def init(self):
        """Abstract method to initialize a controller."""

    @abstractmethod
    async def start(self):
        """Abstract method to start a controller."""

    async def _start_serial(self):
        """Starts the serial listener thread in a coroutine."""
        queue = asyncio.Queue()
        event_loop = asyncio.get_event_loop()

        def on_byte_received(byte):
            """Callback called on byte received."""
            event_loop.call_soon_threadsafe(queue.put_nowait, byte)

        self.serial = SerialInterface(
            self.settings.port, self.settings.baudrate, on_byte_received
        )

        while 1:
            byte = await queue.get()
            self.handle_byte(byte)

    async def _open_webbrowser(self):
        """Wait until the server is ready before opening a web browser."""
        while 1:
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", 8000)
            except ConnectionRefusedError:
                await asyncio.sleep(0.1)
            else:
                writer.close()
                break
        if self.settings.webbrowser is True:
            webbrowser.open("http://localhost:8000/dotbots")

    async def _dotbots_update(self):
        """Coroutine that periodically updates the list of known dotbots."""
        while 1:
            to_remove = []
            for dotbot in self.dotbots.values():
                if dotbot.last_seen + 3 < time.time():
                    to_remove.append(dotbot)
            for dotbot in to_remove:
                self.dotbots.pop(dotbot.address)
            if to_remove:
                await self.notify_clients(json.dumps({"cmd": "reload"}))
            await asyncio.sleep(1)

    async def _dotbots_table_refresh(self):
        """Display and refresh a table of known dotbots."""

        def table():
            table = Table()
            if self.dotbots:
                table.add_column("#", style="cyan")
                table.add_column("address", style="magenta")
                table.add_column("last seen", style="green")
                table.add_column("active", style="green")
                for idx, dotbot in enumerate(self.dotbots.values()):
                    table.add_row(
                        f"{idx:<4}",
                        f"0x{dotbot.address}",
                        f"{dotbot.last_seen:.3f}",
                        f"{int(dotbot.address, 16) == self.header.destination}",
                    )
            return table

        with Live(table(), refresh_per_second=10) as live:
            while 1:
                live.update(table())
                await asyncio.sleep(1)

    def handle_byte(self, byte):
        """Called on each byte received over UART."""
        self.hdlc_handler.handle_byte(byte)
        if self.hdlc_handler.state == HDLCState.READY:
            payload = self.hdlc_handler.payload
            if payload:
                try:
                    payload = ProtocolPayload.from_bytes(payload)
                except ProtocolPayloadParserException:
                    print(f"Cannot parse payload '{payload}'")
                    return
                self.handle_received_payload(payload)

    def handle_received_payload(self, payload: ProtocolPayload):
        """Handle a received payload."""
        # Controller is not interested by command messages received
        if payload.payload_type in [
            PayloadType.CMD_MOVE_RAW,
            PayloadType.CMD_RGB_LED,
        ]:
            return
        if self.settings.verbose:
            print(payload)
        source = hexlify(int(payload.header.source).to_bytes(8, "big")).decode()
        dotbot = DotBotModel(
            address=source,
            last_seen=time.time(),
            active=(int(source, 16) == self.header.destination),
        )
        if payload.payload_type == PayloadType.LH2_RAW_DATA:
            if self.settings.calibrate:
                calibration_csv = os.path.join(
                    self.settings.calibration_dir, "calibration.csv"
                )
                save_camera_points(payload.values, calibration_csv)
            if self.calibration is not None:
                coordinates = compute_coordinates(payload.values, self.calibration)
                if coordinates is not None:
                    # print("coordinates", coordinates)
                    asyncio.create_task(
                        self.notify_clients(
                            json.dumps(
                                {
                                    "cmd": "lh2_position",
                                    "address": dotbot.address,
                                    "x": coordinates[0],
                                    "y": coordinates[1],
                                }
                            )
                        )
                    )
        if source not in self.dotbots:
            asyncio.create_task(self.notify_clients(json.dumps({"cmd": "reload"})))
        self.dotbots.update({dotbot.address: dotbot})

    async def _ws_send_safe(self, websocket: WebSocket, msg: str):
        """Safely send a message to a websocket client."""
        try:
            await websocket.send_text(msg)
        except websockets.exceptions.ConnectionClosedError:
            await asyncio.sleep(0.1)

    async def notify_clients(self, message):
        """Send a message to all clients connected."""
        await asyncio.gather(
            *[self._ws_send_safe(websocket, message) for websocket in self.websockets]
        )

    def send_payload(self, payload: ProtocolPayload):
        """Sends a command in an HDLC frame over serial."""
        destination = hexlify(
            int(payload.header.destination).to_bytes(8, "big")
        ).decode()
        if destination not in self.dotbots:
            return
        if self.serial is not None:
            self.serial.write(hdlc_encode(payload.to_bytes()))

    async def run(self):
        """Launch the controller."""
        try:
            self.init()
            tasks = [
                asyncio.create_task(web(self)),
                asyncio.create_task(self._open_webbrowser()),
                asyncio.create_task(self._start_serial()),
                asyncio.create_task(self._dotbots_update()),
                asyncio.create_task(self._dotbots_table_refresh()),
                asyncio.create_task(self.start()),
            ]
            await asyncio.gather(*tasks)
        except (
            SystemExit,
            SerialInterfaceException,
            serial.serialutil.SerialException,
        ) as exc:
            print(f"{exc}")
            for task in tasks:
                task.cancel()


def register_controller(type_, cls):
    """Register a new controller."""
    CONTROLLERS.update({type_: cls})


def controller_factory(type_, settings: ControllerSettings):
    """Returns an instance of a concrete Dotbot controller."""
    if type_ not in CONTROLLERS:
        raise ControllerException("Invalid controller")
    return CONTROLLERS[type_](settings)
