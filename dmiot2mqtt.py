#!/usr/bin/env python3
"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import asyncio
import configparser
import json
import logging
from os import path as path_joiner
import uuid

try:
    import aiomqtt
except ImportError:
    exit("aiomqtt library is missing, quitting...")
    

HOST = '0.0.0.0'
PORT = 31270

logger = logging.getLogger("dmoit2mqtt")


class DreamMakerIotClient:
    """
    Wrapper which handles the communication for one client connection.
    """
    REPLY_TEMPLATE =  {"action": 81, "resource_id": 0, "version":"zeico_3.0.0", "code": 0}
    # @attention: Data dict is missing and needs to be inserted!
    COMMAND_TEMPLATE = {"action":4, "resource_id": 9031, "version":"zeico_3.0.0", "msg_id": 0}
    
    RESOURCE_STATUS = 127
    
    MAP_MODES = {
        0: "direct",
        1: "natural",
        2: "smart"
    }
    
    def __init__(self, stream_reader: asyncio.StreamReader, stream_writer: asyncio.StreamWriter):
        addr = stream_writer.get_extra_info('peername')
        logger.info(f"Client {addr!r} connected.")
        self.device_id = '000000000000000000000000'
        self.device_key = '0000000000000000'
        self.client_ip = addr[0]
        self.stream_reader = stream_reader
        self.stream_writer = stream_writer
        # self.mqtt_client = MqttConfig.get_client()

    async def async_authenticate_client(self) -> bool:
        while not self.is_connection_closed():
            message = await self.async_get_data()
            if not message:
                logger.warning("Empty message...")
                continue
            # Provisioning request
            if message["action"] == 1 and message["resource_id"] == 2000:
                logger.info("Received provisioning request, sending data...")
                logger.debug(message)
                await self.async_send_provisioning_data(message)
            # Auth request
            elif message["action"] == 1 and message["resource_id"] == 2001:
                self.device_id = message['data']['device_id']
                self.device_key = message['data']['device_key']
                logger.info(f"Auth handshake for Device Key '{self.device_key}' with Device ID '{self.device_id}'")
                logger.debug(message)
                if MqttConfig.home_assistant_mqtt_discovery: await self.async_discovery_registry()
                await self.async_publish_mqtt_availability(True)
                await self.async_ack_message(message)
                return True
        return False
    
    def is_connection_closed(self):
        return self.stream_reader.at_eof()
    
    async def async_get_data(self) -> dict:
         data = await self.stream_reader.read(1024)
         if not data:
             return {}
         else:
             try:
                 return json.loads(data.decode())
             except json.decoder.JSONDecodeError:
                 logger.warning("Invalid JSON received, ignoring...")
                 return {}
    
    async def async_send_data(self, data: dict):
        message = json.dumps(data).encode()
        self.stream_writer.write(message)
        logger.debug("Sending: {}".format(message))
        await self.stream_writer.drain()
        
    async def async_send_command(self, command_data: dict):
        message = self.COMMAND_TEMPLATE.copy()
        message["data"] = command_data
        await self.async_send_data(message)
    
    async def async_ack_message(self, message: dict):
        """
        Each incoming message needs to be either ACK'ed or answered properly.
        """
        response = self.REPLY_TEMPLATE.copy()
        response["resource_id"] = message["resource_id"]
        await self.async_send_data(response)
        
    async def async_discovery_registry(self):
        """
        Construct an send initial mqtt discovery messages.
        """
        discovery_json_text = open('discovery.json', "r").read()
        discovery_json_text = discovery_json_text.replace("<DEVICE_ID>", self.device_id)
        if "DM-" in self.device_key:
            discovery_json_text = discovery_json_text.replace("<DEVICE_KEY>", self.device_key)
        else:
            discovery_json_text = discovery_json_text.replace("<DEVICE_KEY>", "DreamMaker device")
        discovery_json_dict = json.loads(discovery_json_text)
        # loop discovery info to registry every entity
        for entity_type, entities in discovery_json_dict.items():
            for config in entities:
                entity_short_name = config['unique_id'].replace(f"{self.device_id}_", "")
                discovery_mqtt_topic = path_joiner.join(MqttConfig.discovery_topic, entity_type, self.device_id.lower(), entity_short_name, 'config')
                # publish message over mqtt
                async with aiomqtt.Client(**MqttConfig.get_mqtt_client_kwargs()) as mqtt_client:
                    await mqtt_client.publish(discovery_mqtt_topic, payload=json.dumps(config).encode(), retain = True)
                log_msg = f"Entity '{entity_short_name}' ({entity_type}) of device '{self.device_id}', registered"
                if logger.level != getattr(logging, 'DEBUG'.upper()):
                    logger.info(log_msg)
                else:
                    logger.debug(f"{log_msg} on topic '{discovery_mqtt_topic}'")
                    
    async def async_publish_mqtt_availability(self, online: bool):        
        async with aiomqtt.Client(**MqttConfig.get_mqtt_client_kwargs()) as mqtt_client:
            mqtt_availability_topic = path_joiner.join(MqttConfig.base_topic, self.device_id.lower(), "availability")
            data = {"state": "online" if online else "offline"}
            logger.debug(f"Sending MQTT available '{data['state']}' to topic: {mqtt_availability_topic}")
            await mqtt_client.publish(mqtt_availability_topic, payload=json.dumps(data).encode())
    
    async def async_send_provisioning_data(self, incoming_msg: dict):
        response = self.REPLY_TEMPLATE.copy()
        new_device_id = uuid.uuid4().hex[:24]
        new_device_key = f"DM-{incoming_msg['data']['product_model'].upper()}"
        response["resource_id"] = 2000
        response["action"] = 81
        response["data"] = {"device_key": new_device_key, "device_id": new_device_id}
        await self.async_send_data(response)
        
    async def async_mqtt_listen(self):
        async with aiomqtt.Client(**MqttConfig.get_mqtt_client_kwargs()) as mqtt_client:
            mqtt_base_topic = path_joiner.join(MqttConfig.base_topic, self.device_id.lower())
            mqtt_command_topic = path_joiner.join(mqtt_base_topic, "command")

            # Async MQTT subscription
            await mqtt_client.subscribe(mqtt_command_topic)
            logger.debug("MQTT subscribe to %s finish", mqtt_command_topic)

            async for message in mqtt_client.messages:
                logger.debug("Received command {}".format(message.payload.decode()))
                command = json.loads(message.payload.decode())
                await self.async_send_command(command)

    async def async_run(self):
        """
        Main entry point for a new client connection.
        """        
        if not await self.async_authenticate_client():
            return False
        
        # Schedule two tasks, once one of them completes we need to take action
        tcp_task = asyncio.create_task(self.async_get_data())
        # mqtt_task = asyncio.create_task(self.mqtt_client.deliver_message())
        mqtt_task = asyncio.create_task(self.async_mqtt_listen())
        
        ##
        # Main communication loop
        ##
        while not self.is_connection_closed():
            # run tasks until one of them completes
            done, pending = await asyncio.wait({tcp_task, mqtt_task}, return_when=asyncio.FIRST_COMPLETED)
        
            if tcp_task in done:
                logger.debug("TCP task done, rescheduling....")
                message = tcp_task.result()
                # Make sure to create a new TCP reader task first
                tcp_task = asyncio.create_task(self.async_get_data())
            
                if message:
                    logger.debug(message)            
                    await self.async_ack_message(message)
                    # Don't spam MQTT server with status/heartbeat values
                    if message["resource_id"] == self.RESOURCE_STATUS:
                        continue
                    data = message["data"]
                    # publish message over mqtt
                    await self.async_publish_mqtt_availability(True)
                    async with aiomqtt.Client(**MqttConfig.get_mqtt_client_kwargs()) as mqtt_client:
                        mqtt_base_topic = path_joiner.join(MqttConfig.base_topic, self.device_id.lower())
                        await mqtt_client.publish(mqtt_base_topic, payload=json.dumps(data).encode())
            
            if mqtt_task in done:
                logger.debug("MQTT task done, rescheduling....")
                message = mqtt_task.result()
                mqtt_task = asyncio.create_task(self.async_mqtt_listen())
        
    async def async_stop(self):
        logger.info("Quit....")
        self.stream_writer.close()
        await self.async_publish_mqtt_availability(False)


class MqttConfig:
    server = "localhost"
    port = 1883
    base_topic = "dmiot2mqtt"
    user = None
    password = None
    use_ssl = False
    home_assistant_mqtt_discovery = True
    discovery_topic = "homeassistant"
    retain = False
    
    @classmethod
    def get_uri(cls):
        if cls.use_ssl:
            proto = "mqtts"
        else:
            proto = "mqtt"
        
        uri = f"{proto}://"
        if cls.user:
            uri += cls.user
            if cls.password:
                uri += ":" + cls.password
            uri += "@"
        uri += f"{cls.server}:{cls.port}/"
        return uri

    @classmethod
    def read_config(cls, config_file):
        """
        Reads MQTT config values from the specified ini file.
        """
        config = configparser.ConfigParser()
        config.read(config_file)
        if "mqtt" not in config:
            logger.warning("No MQTT config found, falling back to localhost")
            return

        cls.server = config["mqtt"].get("server", cls.server)
        cls.port = config["mqtt"].get("port", cls.port)
        cls.base_topic = config["mqtt"].get("base_topic", cls.base_topic)
        cls.user = config["mqtt"].get("user", None)
        cls.password = config["mqtt"].get("password", None)
        cls.use_ssl = config["mqtt"].getboolean("use_ssl", False)
        cls.home_assistant_mqtt_discovery = config["mqtt"].getboolean("home_assistant_mqtt_discovery", True)
        cls.discovery_topic = config["mqtt"].get("discovery_topic", cls.discovery_topic)
        cls.retain = config["mqtt"].getboolean("retain", False)
    
    @classmethod
    def get_mqtt_client_kwargs(cls) -> dict:
        return {
            'hostname': cls.server,
            #'port': cls.port,
            'username': cls.user,
            'password': cls.password,
            'clean_session': True,
        }


async def client_connected_callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    dreammakeriotclient = DreamMakerIotClient(reader, writer)
    await dreammakeriotclient.async_run()
    await dreammakeriotclient.async_stop()


async def main():
    server = await asyncio.start_server(client_connected_callback, HOST, PORT)
    
    addr = server.sockets[0].getsockname()
    logger.info(f'Serving on {addr[0]}:{addr[1]}')

    async with server:
        await server.serve_forever()


if __name__ == "__main__":

    def argparse_type_loglevel(level):
        """
        Type check for log level.
        """
        numeric_level = getattr(logging, level.upper(), None)
        if not isinstance(numeric_level, int):
            raise ValueError('Invalid log level: %s' % level)
        else:
            return numeric_level
    
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", nargs='?', default="dmiot2mqtt.ini", help="Config file location")
    parser.add_argument("-l", "--loglevel", default="info", nargs='?', help="Sets the log level (DEBUG, INFO, WARNING, ERROR). Default: INFO", type=argparse_type_loglevel)
    args = parser.parse_args()
    
    # Set up logging
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - [%(name)-10s] %(levelname)-8s %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(args.loglevel)
    
    # Read config and store it in global class scope
    MqttConfig.read_config(args.config)

    # Async main loop
    asyncio.run(main())
