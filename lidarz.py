#!/usr/bin/env python
import os
import argparse
import configparser
import logging
import asyncio
import json
import serial_asyncio_fast
import numpy as np
import struct
from enum import Enum
from json import JSONEncoder
from shapely.geometry import Point, Polygon
from shapely.prepared import prep
from aiohttp import web, WSMsgType
from aiortc import RTCPeerConnection, RTCSessionDescription

MESSAGE_LENGTH = 47

MEASUREMENT_LENGTH = 12 
MESSAGE_FORMAT = "<xBHH" + "HB" * MEASUREMENT_LENGTH + "HHB"

logger = logging.getLogger("lidarz.py")

State = Enum("State", ["SYNC0", "SYNC1", "SYNC2", "LOCKED", "UPDATE_PLOT", "WS_SEND"])

ROOT = os.path.dirname(__file__)
WEB = os.path.join(ROOT, 'web')

lidars = []

wrtc_pc = None
wrtc_dc = None

ws_client = None

# Lidar Serial read

class LidarSerialProtocol(asyncio.Protocol):
    name = None
    prepared_polygon = None
    offset = np.array([0.0, 0.0])

    def __init__(self):
        super().__init__()
        self.transport = None
        self.state = State.SYNC0
        self.lidar_message = b'' 
        self.lidar_message_pos = 0
        self.polar_coords = []
        self.next_polar_coords = []

    def connection_made(self, transport):
        logger.debug('port opened')
        self.transport = transport

    def connection_lost(self, exc):
        logger.debug('port closed')
        self.transport.loop.stop()

    def data_received(self, serial_data):
        global wrtc_dc, wrtc_pc, ws_client
        serial_data_pos = 0
        serial_data_len = len(serial_data)
        while serial_data_pos < serial_data_len:
            if self.state == State.SYNC0:
                self.lidar_message = b''
                self.lidar_message_pos = 0
                if serial_data[serial_data_pos:serial_data_pos+1] == b'\x54':
                    self.lidar_message = b'\x54'
                    self.lidar_message_pos += 1
                    self.state = State.SYNC1
                else:
                    logger.debug(self.state, '\033[93m\033[1m' + "WARNING: Syncing" + '\033[0m', self.lidar_message, serial_data_len, serial_data_pos, len(self.lidar_message), self.lidar_message_pos)
                serial_data_pos += 1

            elif self.state == State.SYNC1:
                if serial_data[serial_data_pos:serial_data_pos+1] == b'\x2C':
                    self.lidar_message += b'\x2C'
                    self.lidar_message_pos += 1
                    self.state = State.SYNC2
                else:
                    logger.debug(self.state, '\033[93m\033[1m' + "WARNING: Second byte not expected" + '\033[0m', self.lidar_message, serial_data_len, serial_data_pos, len(self.lidar_message), self.lidar_message_pos)
                    self.state = State.SYNC0
                serial_data_pos += 1

            elif self.state == State.SYNC2:
                if (serial_data_len - serial_data_pos) < (MESSAGE_LENGTH - self.lidar_message_pos):
                    self.lidar_message += serial_data[serial_data_pos:]
                    self.lidar_message_pos += (serial_data_len - serial_data_pos)
                    serial_data_pos = serial_data_len
                    logger.debug(self.state, "Message LIDAR to be completed", self.lidar_message, serial_data_len, serial_data_pos, len(self.lidar_message), self.lidar_message_pos)
                else:
                    self.lidar_message += serial_data[serial_data_pos:(serial_data_pos + MESSAGE_LENGTH - self.lidar_message_pos)]
                    serial_data_pos += (MESSAGE_LENGTH - self.lidar_message_pos)
                    lidar_message_parsed = self.parse_lidar_data(self.lidar_message)
                    if self.polar_coords != []:
                        if lidar_message_parsed[0][0] < self.polar_coords[-1][0]:
                            new_polar_coords = True
                        else:
                            new_polar_coords = False
                    else:
                        new_polar_coords = False
                    for lidar_message_index in range(1, len(lidar_message_parsed)):
                        if lidar_message_parsed[lidar_message_index][0] < 360.0:
                            if new_polar_coords:
                                self.next_polar_coords.append(lidar_message_parsed[lidar_message_index])
                            else:
                                self.polar_coords.append(lidar_message_parsed[lidar_message_index])
                        else:
                            self.next_polar_coords.append(tuple((lidar_message_parsed[lidar_message_index][0] - 360, lidar_message_parsed[lidar_message_index][1])))
                    if self.next_polar_coords == []:
                        self.state = State.SYNC0
                    else:
                        self.state = State.WS_SEND

                    logger.debug(self.state, "Message LIDAR complete", self.lidar_message, serial_data_len, serial_data_pos, len(self.lidar_message), self.lidar_message_pos)

            elif self.state == State.WS_SEND:
                cartesian_coords = self.get_xy_data(self.polar_coords)
                numpyData = {self.name: cartesian_coords}
                encodedNumpyData = json.dumps(numpyData, cls=NumpyArrayEncoder)
                if wrtc_dc != None:
                    if wrtc_dc.readyState == "open":
                        wrtc_dc.send(encodedNumpyData)
                if ws_client != None:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.create_task(ws_client.send_str(encodedNumpyData))
                    else:
                        loop.run_until_complete(ws_client.send_str(encodedNumpyData))

                self.polar_coords = list(tuple(self.next_polar_coords))
                self.next_polar_coords = []
                self.state = State.SYNC0


    def parse_lidar_data(self, data):
        length, speed, start_angle, *pos_data, stop_angle, timestamp, crc = \
            struct.unpack(MESSAGE_FORMAT, data)
        start_angle = float(start_angle) / 100.0
        stop_angle = float(stop_angle) / 100.0
        if stop_angle < start_angle:
            stop_angle += 360.0
        step_size = (stop_angle - start_angle) / (MEASUREMENT_LENGTH - 1)
        angle = [start_angle + step_size * i for i in range(0,MEASUREMENT_LENGTH)]
        distance = pos_data[0::2]
        return list(zip(angle, distance))


    def get_xy_data(self, measurements):
        angle = np.array([measurement[0] for measurement in measurements])
        distance = np.array([measurement[1] for measurement in measurements])

        x = np.sin(np.radians(angle)) * (distance / 1000.0)
        y = np.cos(np.radians(angle)) * (distance / 1000.0)
        stackxy = np.dstack((x, y))[0]

        result = self.filter_coordinates_in_polygon(stackxy)
        return result


    def filter_coordinates_in_polygon(self, coordinates):
        filtered_coordinates = np.array([point.tolist() for point in coordinates if self.prepared_polygon.contains(Point(point))]) + self.offset
        return filtered_coordinates


class NumpyArrayEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return JSONEncoder.default(self, obj)


async def webrtc_handler(request):
    global wrtc_pc, wrtc_dc

    wrtc_pc = RTCPeerConnection()
    wrtc_dc = wrtc_pc.createDataChannel("lidar", negotiated=True, ordered=True, id=2)

    @wrtc_dc.on("open")
    def on_open():
        logger.debug("Data channel opened")

    @wrtc_dc.on("close")
    def on_close():
        logger.debug("Data channel closed")
        dc = None

    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    logger.debug("Created for", request.remote)

    @wrtc_pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.debug("Connection state is", wrtc_pc.connectionState)
        if (wrtc_pc.connectionState in ["closed", "failed", "disconnected"] ):
            await wrtc_pc.close()

    await wrtc_pc.setRemoteDescription(offer)

    answer = await wrtc_pc.createAnswer()
    await wrtc_pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"sdp": wrtc_pc.localDescription.sdp, "type": wrtc_pc.localDescription.type}
        ),
    )

async def websocket_handler(request):
    global ws_client
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    ws_client = ws
    print("Client connecté")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                print(f"Message reçu : {msg.data}")
            elif msg.type == WSMsgType.ERROR:
                print(f"Erreur WebSocket : {ws.exception()}")
    finally:
        ws_client = None
        print("Client déconnecté")

    return ws

async def index(request):
    content = open(os.path.join(WEB, "index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)


async def main():
    global wrtc_dc, wrtc_pc, lidars, ws_client

    parser = argparse.ArgumentParser(
        prog="Lidar WebRTC",
        description="OKDO LD06 LIDAR to WebRTC Data Channel",
        epilog="Have fun !"
    )
    parser.add_argument("-v", "--verbose", action="count")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    config = configparser.ConfigParser()
    config.read('lidarz.ini')

    loop = asyncio.get_event_loop()

    for section in config.sections():
        if section == "WEBSERVER":
            server_host = config["WEBSERVER"].get("server-host", "0.0.0.0")
            server_port = config["WEBSERVER"].getint("server-port", 8080)
        elif section == "WEBRTC":
            webrtc_enabled = config["WEBRTC"].getboolean("enable", True)
        elif section == "WEBSOCKET":
            websocket_enabled = config["WEBSOCKET"].getboolean("enable", True)
        elif section.startswith("LIDAR"):
            serial_port = config[section].get("serial-port", "/dev/ttyUSB0")
            serial_baudrate = config[section].getint("serial-baudrate", 230400)
            filter = config[section].get("filter", "[(-12.0, -12.0), (-12.0, 12.0), (12.0, 12.0), (12.0, -12.0)]")
            offset = config[section].get("offset", "[0.0, 0.0]")

            lidar_filter = eval(filter)
            polygon = Polygon(lidar_filter)    
            prepared_polygon = prep(polygon)
            lidar_offset = np.array(eval(offset))
            lidars.append(await serial_asyncio_fast.create_serial_connection(loop, type(str(section), (LidarSerialProtocol,),{"name": section, "prepared_polygon": prepared_polygon, "offset": lidar_offset}), url=serial_port, baudrate=serial_baudrate))

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_static('/', WEB, show_index=False)
    if webrtc_enabled:
        app.router.add_post("/wrtc", webrtc_handler)
    if websocket_enabled:
        app.router.add_get('/ws', websocket_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=server_host, port=server_port)
    await site.start()

    logger.info("Server started. Point browser to " + site.name)

    await asyncio.Event().wait()
    loop.close()


if __name__ == "__main__":
    asyncio.run(main())
    