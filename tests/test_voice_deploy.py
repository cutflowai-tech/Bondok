"""Telephone deployment files: the ElevenLabs agent settings that carry correctness and privacy, the dialplan
identity rule, the route script's scope, and the service/env templates (no secrets, no addresses)."""
import importlib.util
import json
import re
import unittest

from support import ROOT

VOICE = ROOT / 'deploy' / 'voice'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AgentSettings(unittest.TestCase):
    def setUp(self):
        self.body = load('el_agent', VOICE / 'el_agent.py').agent_body('https://h/v1/chat/completions', 'sec')

    def test_only_final_turns_and_no_backup_model(self):
        cc = self.body['conversation_config']
        self.assertIs(cc['turn']['speculative_turn'], False)
        self.assertNotEqual(cc['turn']['turn_eagerness'], 'eager')
        p = cc['agent']['prompt']
        self.assertEqual(p['llm'], 'custom-llm')
        self.assertEqual(p['backup_llm_config'], {'preference': 'disabled'})
        self.assertEqual(p['custom_llm']['api_key'], {'secret_id': 'sec'})
        self.assertEqual(p['timezone'], 'Africa/Cairo')

    def test_privacy_and_limits(self):
        ps = self.body['platform_settings']
        self.assertIs(ps['privacy']['record_voice'], False)
        self.assertIs(ps['privacy']['delete_audio'], True)
        # ElevenLabs refuses zero-retention mode with a custom LLM: shortest retention, audio and transcript deleted
        self.assertIs(ps['privacy']['zero_retention_mode'], False)
        self.assertEqual(ps['privacy']['retention_days'], 1)
        self.assertIs(ps['privacy']['delete_transcript_and_pii'], True)
        zrm = load('el_agent2', VOICE / 'el_agent.py').agent_body('https://h', 's', zero_retention=True)
        self.assertEqual(zrm['platform_settings']['privacy']['retention_days'], -1)   # what ZRM requires
        self.assertIs(ps['auth']['enable_auth'], True)
        self.assertIs(ps['overrides']['custom_llm_extra_body'], True)
        self.assertLessEqual(ps['call_limits']['agent_concurrency_limit'], 2)
        self.assertIs(ps['call_limits']['bursting_enabled'], False)       # no calls above the limit at burst price
        events = self.body['conversation_config']['conversation']['client_events']
        self.assertNotIn('user_transcript', events)
        self.assertNotIn('agent_response', events)

    def test_voice_is_the_chosen_egyptian_male_voice(self):
        self.assertEqual(self.body['conversation_config']['tts']['voice_id'], 'ihycSANIrpHfhWoaq1g3')


class Dialplan(unittest.TestCase):
    def setUp(self):
        self.conf = (VOICE / 'extensions_bondok_voice.conf').read_text()

    def test_identity_is_the_authenticated_endpoint_never_caller_id(self):
        code = '\n'.join(l for l in self.conf.splitlines() if not l.lstrip().startswith(';'))
        self.assertIn('${CHANNEL(endpoint)}', code)
        self.assertNotIn('CALLERID', code)
        self.assertIn('127.0.0.1:9020', code)            # Bondok's bridge, not Sara's 9019
        self.assertNotIn('9019,', code)

    def test_only_111_is_routed_and_the_fixture_is_not_dialable(self):
        exts = re.findall(r'^exten => (\w+),', self.conf, re.M)
        self.assertEqual(sorted(set(exts)), ['111', 's'])
        self.assertNotRegex(self.conf, r'(?m)^\s*include\s*=>')     # nothing here makes the fixture dialable
        script = (VOICE / 'pbx_route.sh').read_text()
        self.assertIn('include => bondok-voice', script)
        self.assertNotIn('bondok-voice-fixture', script)
        self.assertIn("dialplan reload", script)
        self.assertNotIn('core restart', script)
        self.assertNotIn('voice-monday', script)          # never touches Sara's route


class Templates(unittest.TestCase):
    def test_no_secrets_or_addresses_in_templates(self):
        for p in VOICE.iterdir():
            if p.suffix in ('.example', '.json', '.conf', '.yml', '.sh') or p.name.endswith('.example'):
                t = p.read_text()
                self.assertIsNone(re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', t.replace('127.0.0.1', '')), p.name)
                self.assertIsNone(re.search(r'(sk_|xi-|Bearer )[A-Za-z0-9]{16,}', t), p.name)

    def test_access_example_maps_no_office_extension(self):
        data = json.loads((VOICE / 'voice-access.example.json').read_text())
        self.assertEqual(list(data['extensions']), ['99019'])

    def test_voice_env_has_no_slack_token(self):
        t = (VOICE / 'voice.env.example').read_text()
        self.assertNotIn('SLACK_BOT_TOKEN', t)
        self.assertNotIn('SLACK_APP_TOKEN', t)


if __name__ == '__main__':
    unittest.main()
