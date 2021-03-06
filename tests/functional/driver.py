import thread
import threading
import json
import re
import time
import slacker
import websocket


class Driver(object):
    """Function tests driver. It handles the communication with slack api, so that
    the tests code can concentrate on higher level logic.
    """
    def __init__(self, driver_apitoken, driver_username, testbot_username, channel):
        self.slacker = slacker.Slacker(driver_apitoken)
        self.driver_username = driver_username
        self.driver_userid = None
        self.test_channel = channel
        self.users = {}
        self.testbot_username = testbot_username
        self.testbot_userid = None
        # public channel
        self.cm_chan = None
        # direct message channel
        self.dm_chan = None
        self._start_ts = time.time()
        self._websocket = None
        self.events = []
        self._events_lock = threading.Lock()

    def start(self):
        self._rtm_connect()
        # self._fetch_users()
        self._start_dm_channel()
        self._join_test_channel()

    def wait_for_bot_online(self):
        self._wait_for_bot_presense(True)

    def wait_for_bot_offline(self):
        self._wait_for_bot_presense(False)

    def _wait_for_bot_presense(self, online):
        for _ in xrange(10):
            time.sleep(2)
            if online and self._is_testbot_online():
                break
            if not online and not self._is_testbot_online():
                break
        else:
            raise AssertionError('test bot is still %s' % ('offline' if online else 'online'))

    def send_direct_message(self, msg):
        self._send_message_to_bot(self.dm_chan, msg)

    def send_channel_message(self, msg, tobot=True, colon=True):
        colon = ':' if colon else ''
        if tobot:
            msg = '<@%s>%s %s' % (self.testbot_userid, colon, msg)
        self._send_message_to_bot(self.cm_chan, msg)

    def wait_for_bot_direct_message(self, match):
        self._wait_for_bot_message(self.dm_chan, match)

    def wait_for_bot_channel_message(self, match):
        self._wait_for_bot_message(self.cm_chan, match)

    def ensure_no_channel_reply_from_bot_api(self, wait=5):
        for _ in xrange(wait):
            time.sleep(1)
            response = self.slacker.channels.history(
                self.cm_chan, oldest=self._start_ts, latest=time.time())
            for msg in response.body['messages']:
                if self._is_bot_message(msg):
                    raise AssertionError(
                        'expected to get nothing, but got message "%s"' % msg['text'])

    def ensure_no_channel_reply_from_bot_rtm(self, wait=5):
        for _ in xrange(wait):
            time.sleep(1)
            with self._events_lock:
                for event in self.events:
                    if self._is_bot_message(event):
                        raise AssertionError(
                            'expected to get nothing, but got message "%s"' % event['text'])

    ensure_no_channel_reply_from_bot = ensure_no_channel_reply_from_bot_rtm

    def wait_for_file_uploaded(self, name, maxwait=60):
        for _ in xrange(maxwait):
            time.sleep(1)
            if self._has_uploaded_file_rtm(name):
                break
        else:
            raise AssertionError('expected to get file "%s", but got nothing' % name)

    def _send_message_to_bot(self, channel, msg):
        self._start_ts = time.time()
        self.slacker.chat.post_message(channel, msg, username=self.driver_username)

    def _wait_for_bot_message(self, channel, match, maxwait=60):
        for _ in xrange(maxwait):
            time.sleep(1)
            if self._has_got_message_rtm(channel, match):
                break
        else:
            raise AssertionError('expected to get message like "%s", but got nothing' % match)

    def _has_got_message(self, channel, match, start=None, end=None):
        if channel.startswith('C'):
            match = r'\<@%s\>: %s' % (self.driver_userid, match)
        oldest = start or self._start_ts
        latest = end or time.time()
        func = self.slacker.channels.history if channel.startswith('C') \
               else self.slacker.im.history
        response = func(channel, oldest=oldest, latest=latest)
        for msg in response.body['messages']:
            if msg['type'] == 'message' and re.match(match, msg['text'], re.DOTALL):
                return True
        return False

    def _has_got_message_rtm(self, channel, match):
        if channel.startswith('C'):
            match = r'\<@%s\>: %s' % (self.driver_userid, match)
        with self._events_lock:
            for event in self.events:
                if event['type'] == 'message' and re.match(match, event['text'], re.DOTALL):
                    return True
            return False

    def _fetch_users(self):
        response = self.slacker.users.list()
        for user in response.body['members']:
            self.users[user['name']] = user['id']

        self.testbot_userid = self.users[self.testbot_username]
        self.driver_userid = self.users[self.driver_username]

    def _rtm_connect(self):
        r = self.slacker.rtm.start().body
        self.driver_username = r['self']['name']
        self.driver_userid = r['self']['id']

        self.users = {u['name']: u['id'] for u in r['users']}
        self.testbot_userid = self.users[self.testbot_username]

        self._websocket = websocket.create_connection(r['url'])
        self._websocket.sock.setblocking(0)
        thread.start_new_thread(self._rtm_read_forever, tuple())

    def _websocket_safe_read(self):
        """Returns data if available, otherwise ''. Newlines indicate multiple messages """
        data = ''
        while True:
            try:
                data += '{0}\n'.format(self._websocket.recv())
            except:
                return data.rstrip()

    def _rtm_read_forever(self):
        while True:
            json_data = self._websocket_safe_read()
            if json_data != '':
                with self._events_lock:
                    self.events.extend([json.loads(d) for d in json_data.split('\n')])
            time.sleep(1)

    def _start_dm_channel(self):
        """Start a slack direct messages channel with the test bot"""
        response = self.slacker.im.open(self.testbot_userid)
        self.dm_chan = response.body['channel']['id']

    def _is_testbot_online(self):
        response = self.slacker.users.get_presence(self.testbot_userid)
        return response.body['presence'] == self.slacker.presence.ACTIVE

    def _has_uploaded_file(self, name, start=None, end=None):
        ts_from = start or self._start_ts
        ts_to = end or time.time()
        response = self.slacker.files.list(user=self.testbot_userid, ts_from=ts_from, ts_to=ts_to)
        for f in response.body['files']:
            if f['name'] == name:
                return True
        return False

    def _has_uploaded_file_rtm(self, name):
        with self._events_lock:
            for event in self.events:
                if event['type'] == 'file_shared' \
                   and event['file']['name'] == name \
                   and event['file']['user'] == self.testbot_userid:
                    return True
            return False

    def _join_test_channel(self):
        response = self.slacker.channels.join(self.test_channel)
        self.cm_chan = response.body['channel']['id']
        self._invite_testbot_to_channel()

    def _invite_testbot_to_channel(self):
        if self.testbot_userid not in self.slacker.channels.info(self.cm_chan).body['channel']['members']:
            self.slacker.channels.invite(self.cm_chan, self.testbot_userid)

    def _is_bot_message(self, msg):
        if msg['type'] != 'message':
            return False
        if not msg.get('channel', '').startswith('C'):
            return False
        return msg.get('user') == self.testbot_userid \
            or msg.get('username') == self.testbot_username

    def clear_events(self):
        with self._events_lock:
            self.events = []
