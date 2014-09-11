# Copyright (c) Aaron Gallagher <_@habnab.it>
# See COPYING for details.

from twisted.application.internet import TimerService
from twisted.application import service
from twisted.internet.error import ConnectionDone, ConnectionLost
from twisted.internet import protocol, defer
from twisted.python import log
from twisted.web.client import ResponseDone, ResponseFailed
from twisted.web.http import PotentialDataLoss
from twisted.words.protocols import irc

import collections
import itertools
import json
import posixpath
import shlex
import re
import urlparse

import youporn


class StringReceiver(protocol.Protocol):
    def __init__(self, byteLimit=None):
        self.bytesRemaining = byteLimit
        self.deferred = defer.Deferred()
        self._buffer = []

    def dataReceived(self, data):
        data = data[:self.bytesRemaining]
        self._buffer.append(data)
        if self.bytesRemaining is not None:
            self.bytesRemaining -= len(data)
            if not self.bytesRemaining:
                self.transport.stopProducing()

    def connectionLost(self, reason):
        if ((reason.check(ResponseFailed) and any(exn.check(ConnectionDone, ConnectionLost)
                                                  for exn in reason.value.reasons))
                or reason.check(ResponseDone, PotentialDataLoss)):
            self.deferred.callback(''.join(self._buffer))
        else:
            self.deferred.errback(reason)


def receive(response, receiver):
    response.deliverBody(receiver)
    return receiver.deferred


class _IRCBase(irc.IRCClient):
    def ctcpQuery(self, user, channel, messages):
        messages = [(a.upper(), b) for a, b in messages]
        irc.IRCClient.ctcpQuery(self, user, channel, messages)

    def noticed(self, user, channel, message):
        pass

    def connectionLost(self, reason):
        self.factory.unestablished()

    def signedOn(self):
        self.channelUsers = collections.defaultdict(set)
        self.nickPrefixes = ''.join(prefix for prefix, _ in self.supported.getFeature('PREFIX').itervalues())
        self.factory.established(self)

    def irc_RPL_NAMREPLY(self, prefix, params):
        channel = params[2].lower()
        self.channelUsers[channel].update(nick.lstrip(self.nickPrefixes) for nick in params[3].split(' '))

    def userJoined(self, user, channel):
        nick, _, host = user.partition('!')
        self.channelUsers[channel.lower()].add(nick)

    def userLeft(self, user, channel):
        nick, _, host = user.partition('!')
        self.channelUsers[channel.lower()].discard(nick)

    def userQuit(self, user, quitMessage):
        nick, _, host = user.partition('!')
        for users in self.channelUsers.itervalues():
            users.discard(nick)

    def userKicked(self, kickee, channel, kicker, message):
        nick, _, host = kickee.partition('!')
        self.channelUsers[channel.lower()].discard(nick)

    def userRenamed(self, oldname, newname):
        for users in self.channelUsers.itervalues():
            if oldname in users:
                users.discard(oldname)
                users.add(newname)


class TheresaProtocol(_IRCBase):
    _lastURL = None
    lastTwitID = None
    channel = None
    channels = None

    sourceURL = 'https://github.com/habnabit/theresa-bot'
    versionName = 'theresa'
    versionNum = 'HEAD'
    versionEnv = 'twisted'

    def __init__(self):
        if self.channels is None:
            self.channels = self.channel,

    def signedOn(self):
        self.join(','.join(self.channels))
        _IRCBase.signedOn(self)

    def scanMessage(self, channel, message):
        pass

    def personallyAddressed(self, user, channel, message):
        pass

    def privmsg(self, user, channel, message):
        if not channel.startswith('#'):
            return

        addressedMatch = re.match(r'(?i)^s*%s\s*[,:> ]+(\S?.*?)[.!?]?\s*$' % (re.escape(self.nickname),), message)
        if addressedMatch:
            self.personallyAddressed(user, channel, addressedMatch.group(1))
            return

        if not message.startswith((',', '!')):
            defer.maybeDeferred(self.scanMessage, channel, message).addErrback(log.err)
            return

        splut = shlex.split(message[1:])
        command, params = splut[0], splut[1:]
        meth = getattr(self, 'command_%s' % (command.lower(),), None)
        if meth is not None:
            if command == 'dongcc':
                d = defer.maybeDeferred(meth, channel, user, *params)
            else:
                d = defer.maybeDeferred(meth, channel, *params)

            @d.addErrback
            def _eb(f):
                self.msg(channel, 'Error in %s: %s' % (command, f.getErrorMessage()))
                return f

            d.addErrback(log.err)

    def messageChannels(self, message, channels):
        for channel in channels:
            self.msg(channel, message)

    def gotWeather(self, weather):
        self.messageChannels(weather, self.channels)

    def command_youporn(self, channel):
        return (
            defer.gatherResults([
                youporn.fetchYoupornComment(),
                youporn.fetchStockImage(500, 500),
            ])
            .addCallback(youporn.overlayYoupornComment)
            .addCallback(youporn.postToImgur)
            .addCallback(lambda r: r['data']['link']
                         .encode()
                         .replace('http:', 'https:'))
            .addCallback(self.messageChannels, [channel]))


class TheresaFactory(protocol.ReconnectingClientFactory):
    protocol = TheresaProtocol

    def __init__(self, agent, reactor=None):
        self.agent = agent
        if reactor is None:
            from twisted.internet import reactor
        self.reactor = reactor
        self._waiting = []

    def established(self, protocol):
        self._client = protocol
        waiting, self._waiting = self._waiting, []
        for d in waiting:
            d.callback(protocol)
        self.resetDelay()

    def unestablished(self):
        self._client = None

    def clientDeferred(self):
        if self._client is not None:
            return defer.succeed(self._client)
        d = defer.Deferred()
        self._waiting.append(d)
        return d


class WeatherService(service.MultiService):
    def __init__(self, agent, target, api_key, zip_code, interval=120):
        service.MultiService.__init__(self)
        self.agent = agent
        self.target = target
        self.url = lambda api: urlparse.urljoin(
            'http://api.wunderground.com/api/',
            posixpath.join(api_key, api, 'q', str(zip_code) + '.json'))
        self.timer = TimerService(interval, self._doPoll)
        self.timer.setServiceParent(self)
        self.lastUpdate = None

    def poll(self):
        self._doPoll(force=True)

    def _doPoll(self, force=False):
        d = self._actuallyDoPoll(force)
        d.addErrback(log.err)
        return d

    @defer.inlineCallbacks
    def _actuallyDoPoll(self, force):
        resp = yield self.agent.request('GET', self.url('conditions'))
        body = yield receive(resp, StringReceiver())
        j = json.loads(body)['current_observation']
        weather = j['weather'].lower()
        if 'cloud' in weather or weather == 'overcast':
            updateKey = False
        else:
            updateKey = weather
        if self.lastUpdate is None:
            self.lastUpdate = updateKey
            return
        if updateKey == self.lastUpdate and not force:
            return
        self.lastUpdate = updateKey
        updateString = (
            ('temperature %(temperature_string)s; '
             'feels like %(feelslike_string)s; ' % j)
            + ('weather: %s' % weather))
        client = yield self.target.clientDeferred()
        client.gotWeather(updateString.encode('utf-8'))

    @defer.inlineCallbacks
    def getRainChance(self):
        resp = yield self.agent.request('GET', self.url('hourly'))
        body = yield receive(resp, StringReceiver())
        j = json.loads(body)['hourly_forecast']
        update = []
        for weekday, remaining in itertools.groupby(j, lambda i: i['FCTTIME']['weekday_name']):
            weekdayUpdate = []
            for pop, forecasts in itertools.groupby(remaining, lambda i: round(float(i['pop']) / 10) * 10):
                forecasts = list(forecasts)
                first, last = forecasts[0]['FCTTIME']['hour'], forecasts[-1]['FCTTIME']['hour']
                if first == last:
                    weekdayUpdate.append('{}h: {:.0f}%'.format(first, pop))
                else:
                    weekdayUpdate.append('{}h-{}h: {:.0f}%'.format(first, last, pop))
            update.append('{}: {}'.format(weekday, ', '.join(weekdayUpdate)))
        updateString = '; '.join(update)
        client = yield self.target.clientDeferred()
        client.gotWeather(updateString.encode('utf-8'))
