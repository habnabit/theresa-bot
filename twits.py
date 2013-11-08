# Copyright (c) Aaron Gallagher <_@habnab.it>
# See COPYING for details.

"I HATE TWITTER"
from twisted.application.service import Service
from twisted.protocols.policies import TimeoutMixin
from twisted.web.http_headers import Headers
from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet.error import TimeoutError
from twisted.web.client import ResponseDone
from twisted.web.http import PotentialDataLoss
from twisted.internet import defer
from twisted.python import log, failure
import oauth2

import urlparse
import theresa
import urllib
import json
import re

defaultSignature = oauth2.SignatureMethod_HMAC_SHA1()
defaultTwitterAPI = 'https://api.twitter.com/1.1/'
defaultStreamingAPI = 'https://userstream.twitter.com/1.1/'

class UnexpectedHTTPStatus(Exception):
    pass

def trapBadStatuses(response, goodStatuses=(200,)):
    if response.code not in goodStatuses:
        raise UnexpectedHTTPStatus(response.code, response.phrase)
    return response

class OAuthAgent(object):
    "An Agent wrapper that adds OAuth authorization headers."
    def __init__(self, agent, consumer, token, signatureMethod=defaultSignature):
        self.agent = agent
        self.consumer = consumer
        self.token = token
        self.signatureMethod = signatureMethod

    def request(self, method, uri, headers=None, bodyProducer=None, parameters=None, addAuthHeader=True):
        """Make a request, optionally signing it.

        Any query string passed in `uri` will get clobbered by the urlencoded
        version of `parameters`.
        """
        if headers is None:
            headers = Headers()
        if parameters is None:
            parameters = {}
        if addAuthHeader:
            req = oauth2.Request.from_consumer_and_token(
                self.consumer, token=self.token,
                http_method=method, http_url=uri, parameters=parameters)
            req.sign_request(self.signatureMethod, self.consumer, self.token)
            for header, value in req.to_header().iteritems():
                # oauth2, for some bozotic reason, gives unicode header values
                headers.addRawHeader(header, value.encode())
        parsed = urlparse.urlparse(uri)
        uri = urlparse.urlunparse(parsed._replace(query=urllib.urlencode(parameters)))
        return self.agent.request(method, uri, headers, bodyProducer)

class TwitterStream(LineOnlyReceiver, TimeoutMixin):
    "Receive a stream of JSON in twitter's weird streaming format."
    def __init__(self, delegate, timeoutPeriod=60):
        self.delegate = delegate
        self.timeoutPeriod = timeoutPeriod
        self.deferred = defer.Deferred(self._cancel)
        self._done = False

    def connectionMade(self):
        "Start the timeout once the connection has been established."
        self.setTimeout(self.timeoutPeriod)
        LineOnlyReceiver.connectionMade(self)

    def _cancel(self, ign):
        "A Deferred canceler that drops the connection."
        if self._done:
            return
        self._done = True
        self.transport.stopProducing()
        self.deferred.errback(defer.CancelledError())

    def dataReceived(self, data):
        "Reset the timeout and parse the received data."
        self.resetTimeout()
        LineOnlyReceiver.dataReceived(self, data)

    def lineReceived(self, line):
        "Ignoring empty-line keepalives, inform the delegate about new data."
        if not line:
            return
        obj = json.loads(line)
        try:
            self.delegate(obj)
        except:
            log.err(None, 'error in stream delegate %r' % (self.delegate,))

    def timeoutConnection(self):
        "We haven't received data in too long, so drop the connection."
        if self._done:
            return
        self._done = True
        self.transport.stopProducing()
        self.deferred.errback(TimeoutError())

    def connectionLost(self, reason):
        "Report back how the connection was lost unless we already did."
        self.setTimeout(None)
        if self._done:
            return
        self._done = True
        if reason.check(ResponseDone, PotentialDataLoss):
            self.deferred.callback(None)
        else:
            self.deferred.errback(reason)

class Twitter(object):
    "Close to the most minimal twitter interface ever."
    def __init__(self, agent, twitterAPI=defaultTwitterAPI, streamingAPI=defaultStreamingAPI):
        self.agent = agent
        self.twitterAPI = twitterAPI
        self.streamingAPI = streamingAPI

    def _makeRequest(self, whichAPI, method, resource, parameters):
        d = self.agent.request(method, urlparse.urljoin(whichAPI, resource), parameters=parameters)
        d.addCallback(trapBadStatuses)
        return d

    def request(self, resource, method='GET', **parameters):
        """Make a GET request from the twitter 1.1 API.

        `resource` is the part of the resource URL not including the API URL,
        e.g. 'statuses/show.json'. As everything gets decoded by `json.loads`,
        this should always end in '.json'. Any parameters passed in as keyword
        arguments will be added to the URL as the query string. The `Deferred`
        returned will fire with the decoded JSON.
        """
        d = self._makeRequest(self.twitterAPI, method, resource, parameters)
        d.addCallback(theresa.receive, theresa.StringReceiver())
        d.addCallback(json.loads)
        return d

    def stream(self, resource, delegate, **parameters):
        """Receive from the twitter 1.1 streaming API.

        `resource` and keyword arguments are treated the same as the in
        `request`, and `delegate` will be called with each JSON object which is
        received from the stream. The `Deferred` returned will fire when the
        stream has ended.
        """
        d = self._makeRequest(self.streamingAPI, 'GET', resource, parameters)
        d.addCallback(theresa.receive, TwitterStream(delegate))
        return d

class StreamPreserver(Service):
    "Keep a stream connected as a service."
    def __init__(self, twitter, resource, **parameters):
        self.twitter = twitter
        self.resource = resource
        self.parameters = parameters
        self._streamDone = None
        self._delegates = set()

    def __repr__(self):
        return '<StreamPreserver %#x for %r/%r>' % (id(self), self.resource, self.parameters)

    def _connectStream(self, r):
        if isinstance(r, failure.Failure) and r.check(defer.CancelledError):
            log.msg('not reconnecting twitter stream %r' % self)
            return
        log.msg('reconnecting twitter stream %r' % self)
        d = self._streamDone = self.twitter.stream(self.resource, self._streamDelegate, **self.parameters)
        d.addBoth(self._connectStream)
        d.addErrback(log.err, 'error reading from twitter stream %r' % self)
        return r

    def _streamDelegate(self, data):
        for delegate in self._delegates:
            try:
                delegate(data)
            except Exception:
                log.err('error calling delegate %r' % (delegate,))

    def addDelegate(self, delegate):
        "Add a delegate to receive stream data."
        self._delegates.add(delegate)

    def removeDelegate(self, delegate):
        "Remove a previously-added stream data delegate."
        self._delegates.discard(delegate)

    def startService(self):
        "Start reading from the stream."
        if not self.running:
            self._connectStream(None)
        Service.startService(self)

    def stopService(self):
        "Stop reading from the stream."
        ret = None
        if self.running and self._streamDone is not None:
            self._streamDone.cancel()
            ret = self._streamDone
        Service.startService(self)
        return ret


entityReplacements = [
    ('media', 'media_url_https'),
    ('urls', 'expanded_url'),
]

# SERIOUSLY why the FUCK do I have to do this
dumbCrapReplacements = {
    '&amp;': '&',
    '&lt;': '<',
    '&gt;': '>',
}
dumbCrapRegexp = re.compile('|'.join(re.escape(s) for s in dumbCrapReplacements))

def extractRealTwitText(twit):
    "Oh my god why is this necessary."
    if 'retweeted_status' in twit:
        rt = twit['retweeted_status']
        return u'RT @%s: %s' % (rt['user']['screen_name'], extractRealTwitText(rt))
    replacements = sorted(
        (entity['indices'], entity[replacement])
        for entityType, replacement in entityReplacements
        if entityType in twit['entities']
        for entity in twit['entities'][entityType])
    mutableText = list(twit['text'])
    for (l, r), replacement in reversed(replacements):
        mutableText[l:r] = replacement
    text = u''.join(mutableText)
    return dumbCrapRegexp.sub(lambda m: dumbCrapReplacements[m.group()], text)
