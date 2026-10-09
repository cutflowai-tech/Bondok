from dotenv import dotenv_values
from slack_sdk.socket_mode import SocketModeClient
from pathlib import Path
import os
x=dotenv_values('/opt/waset-bondok/.env');client=SocketModeClient(app_token=x['SLACK_APP_TOKEN'])
try:
 client.connect()
 assert client.is_connected()
 print({'slack_websocket_connected':True,'no_messages_sent':True})
finally:client.close()
