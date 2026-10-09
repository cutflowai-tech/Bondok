import sys
from dotenv import dotenv_values
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from core import BOARD
p=sys.argv[1];e=dotenv_values(p)
try:
 assert e['MONDAY_BOARD_ID']==BOARD
 c=WebClient(token=e['SLACK_BOT_TOKEN']);auth=c.auth_test()
 assert auth['team_id']==e['SLACK_TEAM_ID'],'Wrong Slack workspace'
 assert auth['user_id']=='<bondok-bot-user-id>','Bot token is not Bondok'
 assert '-<bondok-app-id>-' in e['SLACK_APP_TOKEN'],'App token is not Bondok'
 info=c.conversations_info(channel=e['SLACK_CHANNEL_ID'])['channel']
 assert info.get('is_private') and info.get('is_member'),'Invite Bondok to the private channel first'
 # Verify the app token can open Socket Mode without displaying its WebSocket URL.
 r=WebClient(token=e['SLACK_APP_TOKEN']).apps_connections_open(app_token=e['SLACK_APP_TOKEN'])
 assert r.get('ok') and r.get('url'),'Socket Mode token failed'
 print('Slack workspace, private channel membership and Socket Mode credentials verified.')
except SlackApiError as ex:
 print('Slack validation failed: '+str(ex.response.get('error','unknown_error')));sys.exit(1)
except Exception as ex:
 print('Validation failed: '+(str(ex) if isinstance(ex,AssertionError) else type(ex).__name__));sys.exit(1)
