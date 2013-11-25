# coding: utf-8

import re

from twisted.internet import protocol
from twisted.python import log

import omoogle
import theresa


class StrangerProtocol(theresa._IRCBase):
    def signedOn(self):
        theresa._IRCBase.signedOn(self)
        self.join(self.factory.channel)

    def joined(self, channel):
        if self.factory.channel == channel:
            self.factory.stranger.setEventCallback(self._omegleEvent)
            self.factory.stranger.waitForState('done').addCallback(self._done)

    def privmsg(self, user, channel, message):
        if channel != self.factory.channel:
            return
        if not user.startswith('Stranger'):
            addressedMatch = re.match(ur'(?iu)^s*%s\s*[‘,:> ]+(\S?.*?)\s*$' % (re.escape(self.nickname),), message)
            if addressedMatch:
                message = addressedMatch.group(1)
            else:
                return

        self.factory.stranger.sendMessage(message.decode('utf-8', 'replace'))

    def _omegleEvent(self, event):
        if event[0] == 'connected':
            self.notice(self.factory.channel, '[connected]')
        elif event[0] == 'commonLikes':
            self.notice(self.factory.channel, '[i like %s]' % (' and '.join(event[1]).encode(),))
        elif event[0] == 'gotMessage':
            self.msg(self.factory.channel, event[1].encode('utf-8'))
        elif event[0] == 'recaptchaRequired':
            self.notice(self.factory.channel, 'recaptcha: %r' % (event[1:],))

    def _done(self, ign):
        self.quit()


class StrangerFactory(protocol.ClientFactory):
    protocol = StrangerProtocol

    def __init__(self, stranger, channel):
        self.stranger = stranger
        self.channel = channel

    def buildProtocol(self, addr):
        print 'connecting', self.stranger
        proto = protocol.ClientFactory.buildProtocol(self, addr)
        proto.nickname = 'Stranger%s' % (self.stranger.strangerID,)
        return proto


class OmoogleTheresa(theresa.TheresaProtocol):
    def __init__(self):
        theresa.TheresaProtocol.__init__(self)
        self.strangers = {}

    def command_newstranger(self, channel, *interests):
        stranger = omoogle.Stranger(self.factory.reactor, self.factory.agent)
        stranger.connect(interests)
        fac = StrangerFactory(stranger, channel)
        self.factory.endpoint.connect(fac)
        self.strangers[stranger.strangerID] = fac

    def command_killstranger(self, channel, stranger):
        self.strangers[stranger].stranger.disconnect().addErrback(log.err, 'error disconnecting %s' % (stranger,))
