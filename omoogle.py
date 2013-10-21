#!/usr/bin/env python
from twisted.web.http_headers import Headers
from twisted.internet import defer, error, task
from twisted.python import log
from twisted.python.failure import Failure

from urllib import urlencode
import collections
import itertools
import urlparse
import json
import random
import datetime
from functools import wraps

from zope.interface import implementer

from twisted.web.iweb import IBodyProducer
from twisted.web.resource import Resource
from twisted.web.template import tags, renderElement

from theresa import StringReceiver, receive
from twatter import trapBadStatuses


class OmoogleError(Exception):
    pass


class InvalidState(OmoogleError):
    pass


class NoServersAvailable(OmoogleError):
    pass


@implementer(IBodyProducer)
class StringProducer(object):
    def __init__(self, body):
        self.body = body
        self.length = len(body)

    def startProducing(self, consumer):
        consumer.write(self.body)
        return defer.succeed(None)

    def pauseProducing(self):
        pass

    def stopProducing(self):
        pass


def receiveJSON(response):
    d = receive(response, StringReceiver())
    d.addCallback(json.loads)
    return d

def setTimeout(d, clock, timeout):
    canceler = clock.callLater(timeout, d.cancel)
    def cancelCanceler(result):
        try:
            canceler.cancel()
        except (error.AlreadyCalled, error.AlreadyCancelled):
            pass
        return result
    d.addBoth(cancelCanceler)

def fetchOmegleStatus(clock, agent, statusURL='http://omegle.com/status', timeout=10):
    d = agent.request('GET', statusURL)
    setTimeout(d, clock, timeout)
    d.addCallback(trapBadStatuses)
    d.addCallback(receiveJSON)
    return d

def generateRandID():
    return ''.join(random.choice('23456789ABCDEFGHJKLMNPQRSTUVWXYZ') for x in xrange(8))


def needsState(*states):
    def deco(f):
        @wraps(f)
        def wrapper(self, *a, **kw):
            if self.state not in states:
                raise InvalidState("can't %s in %r state" % (f.__name__, self.state))
            return f(self, *a, **kw)
        return wrapper
    return deco

def tryEventCallback(callback, event):
    try:
        callback(event)
    except Exception:
        log.err(None, 'error in event callback')

class Stranger(object):
    userAgents = [
        "Mozilla/5.0 (Windows NT 6.1; WOW64; rv:14.0) Gecko/20100101 Firefox/14.0.1",
        "Mozilla/5.0 (Windows; U; Windows NT 6.1; es-AR; rv:1.9) Gecko/2008051206 Firefox/3.0"
    ]
    requestTimeout = 360

    # disconnected -> fetching-status -> requesting-id -> waiting-for-peer -> got-peer -> done
    #                                                           v                 ^
    #                                                     needs-recaptcha --------'
    state = 'disconnected'

    clientID = commonLikes = servers = status = startedAt = None
    recaptchaPublicKey = None

    _counter = itertools.count()

    def __init__(self, clock, agent, eventCallback=None, autoPump=True):
        self.clock = clock
        self.agent = agent
        self.eventCallback = eventCallback
        self.autoPump = autoPump
        self.userAgent = random.choice(self.userAgents)
        self._bufferedEvents = []
        self._requestDeferreds = {}
        self._stateChangeDeferreds = collections.defaultdict(list)
        self._stateResolutions = {}
        self._id = next(self._counter)

    def __repr__(self):
        return '<Stranger %d>' % (self._id,)

    def _setState(self, state, resolution=None):
        self.state = state
        self._stateResolutions[state] = resolution
        for deferred in self._stateChangeDeferreds.pop(state, []):
            deferred.callback(resolution)

    def waitForState(self, state):
        if state in self._stateResolutions:
            return defer.succeed(self._stateResolutions[state])
        d = defer.Deferred()
        self._stateChangeDeferreds[state].append(d)
        return d

    def _disconnect(self, result=None):
        if self.state == 'done':
            return result
        self._setState('done')
        deferreds, self._requestDeferreds = self._requestDeferreds, None
        for requestDeferred in deferreds:
            requestDeferred.cancel()
        return result

    def _chooseNextServer(self):
        if not self.servers:
            log.msg('%s: ran out of servers' % (self,))
            self._disconnect()
            return
        self.server = self.servers.pop(0)
        if len(self.servers) <= 2:
            log.msg('%s: refreshing server list' % (self,))
            d = self._fetchStatus()
            d.addErrback(log.err, 'error refreshing servers')

    def _requestDone(self, result, requestDeferred):
        if self._requestDeferreds is not None:
            self._requestDeferreds.pop(requestDeferred, None)
        return result

    @needsState('requesting-id', 'waiting-for-peer', 'needs-recaptcha', 'got-peer')
    def request(self, whichAPI, data=None, queryArgs=None, retry=True, timeout=None):
        headers = Headers()
        headers.addRawHeader('user-agent', self.userAgent)
        body = None

        if data is not None:
            headers.addRawHeader('content-type', 'application/x-www-form-urlencoded; charset=utf-8')
            body = StringProducer(urlencode(data))

        if timeout is None:
            timeout = self.requestTimeout

        def _retryRequest(failure, server):
            self._chooseNextServer()
            url = 'http://%s/%s' % (server, whichAPI)
            if self.state == 'done':
                log.err(failure, '%s: stranger done; propagating failure from %s' % (self, url))
                return failure
            log.err(failure, '%s: failed request on %s; retrying on a different server' % (self, url))
            return _makeRequest()

        def _makeRequest():
            server = self.server
            url = 'http://%s/%s' % (server, whichAPI)
            if queryArgs:
                parsedUrl = urlparse.urlparse(url)
                url = urlparse.urlunparse(parsedUrl._replace(query=urlencode(queryArgs)))
            d = self.agent.request('POST', url, headers, body)
            self._requestDeferreds[d] = url
            if timeout:
                setTimeout(d, self.clock, timeout)
            d.addBoth(self._requestDone, d)
            d.addErrback(_retryRequest, server)
            d.addErrback(self._disconnect)
            return d

        return _makeRequest()

    @needsState('waiting-for-peer', 'needs-recaptcha', 'got-peer')
    def requestWithID(self, whichAPI, data=None, **kw):
        if data is None:
            data = {}
        data['id'] = self.clientID
        return self.request(whichAPI, data, **kw)

    def setEventCallback(self, eventCallback):
        buffered, self._bufferedEvents = self._bufferedEvents, None
        self.eventCallback = eventCallback
        for event in buffered:
            tryEventCallback(self.eventCallback, event)

    def _parseEvents(self, events):
        if events is None:
            events = [['strangerReallyDisconnected']]
            log.msg('%s: no events' % (self,))
        for event in events:
            handler = getattr(self, '_strangerEvent_%s' % (event[0],), None)
            if handler is not None:
                handler(*event[1:])
            if self.eventCallback is None:
                self._bufferedEvents.append(event)
            else:
                tryEventCallback(self.eventCallback, event)

    def _strangerEvent_strangerReallyDisconnected(self):
        self._disconnect()

    def _strangerEvent_connected(self):
        self._setState('got-peer')

    def _strangerEvent_commonLikes(self, likes):
        self.commonLikes = likes

    @needsState('waiting-for-peer', 'needs-recaptcha')
    def _strangerEvent_recaptchaRequired(self, publicKey):
        self.recaptchaPublicKey = publicKey
        self._setState('needs-recaptcha')

    def _gotID(self, response):
        self.clientID = response['clientID']
        self._setState('waiting-for-peer')
        self._parseEvents(response['events'])
        if self.autoPump:
            self.pumpEvents()

    def _fetchStatus(self):
        d = fetchOmegleStatus(self.clock, self.agent)
        d.addCallback(self._parseStatus)
        return d

    def _parseStatus(self, status):
        self.servers = [server.encode() for server in status['servers']]
        self.status = status
        self._chooseNextServer()

    def _requestID(self, ign, topics, randid):
        args = dict(rcs='1', spid='', firstevents='1')
        if topics:
            args['topics'] = json.dumps(topics)
        if randid:
            args['randid'] = randid
        self._setState('requesting-id')
        d = self.request('start', queryArgs=args)
        d.addCallback(trapBadStatuses)
        d.addCallback(receiveJSON)
        d.addCallback(self._gotID)
        d.addErrback(self._disconnect)
        return d

    @needsState('disconnected')
    def connect(self, topics=None, randid=None):
        self.startedAt = datetime.datetime.now()
        self._setState('fetching-status')
        d = self._fetchStatus()
        d.addCallbacks(self._requestID, self._disconnect, callbackArgs=(topics, randid))
        return d

    def fetchEvents(self, timeout=0):
        d = self.requestWithID('events', timeout=timeout)
        d.addCallback(receiveJSON)
        d.addCallback(self._parseEvents)
        return d

    def pumpEvents(self, ign=None):
        if self.state == 'done':
            return
        self.fetchEvents().addErrback(log.err, '%s: error pumping events' % (self,)).addCallback(self.pumpEvents)

    def _checkActionResponse(self, response):
        def checkWin(s):
            assert s == 'win'
        d = receive(response, StringReceiver())
        d.addCallback(checkWin)
        return d

    def sendMessage(self, msg):
        return self.requestWithID('send', dict(msg=msg.encode('utf-8'))).addCallback(self._checkActionResponse)

    def startedTyping(self):
        return self.requestWithID('typing').addCallback(self._checkActionResponse)

    def stoppedTyping(self):
        return self.requestWithID('stoppedtyping').addCallback(self._checkActionResponse)

    @needsState('needs-recaptcha')
    def solveRecaptcha(self, challenge, response):
        d = self.requestWithID('recaptcha', dict(challenge=challenge, response=response))
        d.addCallback(self._checkActionResponse)
        return d

    @needsState('fetching-status', 'requesting-id', 'waiting-for-peer', 'needs-recaptcha', 'got-peer')
    def disconnect(self):
        def _disconnectionCallback(result):
            if isinstance(result, Failure):
                log.err(result, '%s: error during disconnection notification; disconnecting anyway' % (self,))
            self._disconnect()

        if self.state in ('waiting-for-peer', 'got-peer'):
            d = self.requestWithID('disconnect')
            d.addCallback(self._checkActionResponse)
            d.addBoth(_disconnectionCallback)
            return d
        else:
            self._disconnect()
            return defer.succeed(None)

class StrangerPool(object):
    strangerFactory = Stranger

    def __init__(self, clock, agent, preferredPoolSize=5, stagger=2):
        self.clock = clock
        self.agent = agent
        self.preferredPoolSize = preferredPoolSize
        self.stagger = stagger
        self._looper = task.LoopingCall(self._maybeStartAStranger)
        self._ready = set()
        self._connecting = set()
        self._connected = set()
        self._waiting = []
        self._randids = set()
        self.strangers = {}
        self._strangerCounter = itertools.count()
        self.started = False
        for x in xrange(self.preferredPoolSize):
            self._randids.add(generateRandID())

    def connectStranger(self, s, randid):
        return s.connect(randid=randid)

    def _maybeStartAStranger(self, ign=None):
        if len(self._connecting) + len(self._connected) >= self.preferredPoolSize:
            return
        self._startStranger()

    def _startStranger(self):
        s = self.strangerFactory(self.clock, self.agent, None)
        log.msg('%s: starting' % (s,))
        randid = self._randids.pop()
        self.connectStranger(s, randid)
        self._connecting.add(s)
        strangerID = s.strangerID = str(self._strangerCounter.next())
        self.strangers[strangerID] = s
        s.waitForState('got-peer').addCallback(self._strangerConnected, s)
        s.waitForState('done').addCallback(self._pruneStranger, s, randid)

    def _strangerConnected(self, ign, stranger):
        log.msg('%s: got peer' % (stranger,))
        self._connecting.discard(stranger)
        self._connected.add(stranger)
        if self._waiting:
            d = self._waiting.pop(0)
            d.callback(stranger)
        else:
            self._ready.add(stranger)

    def _pruneStranger(self, ign, stranger, randid):
        log.msg('%s: done' % (stranger,))
        self._connecting.discard(stranger)
        self._ready.discard(stranger)
        self._connected.discard(stranger)
        self._randids.add(randid)
        self.strangers.pop(stranger.strangerID, None)

    def start(self):
        self._looper.start(self.stagger)

    def stop(self):
        self._looper.stop()

    def waitForStranger(self):
        if self.started:
            self._startSomeStrangers()
        d = defer.Deferred()
        if self._ready:
            stranger = self._ready.pop()
            d.callback(stranger)
        else:
            self._waiting.append(d)
        return d

    def waitForNStrangers(self, n):
        if n > self.preferredPoolSize:
            raise ValueError("can't wait for that many strangers")
        return self._waitForNStrangers(n, [])

    def _waitForNStrangers(self, n, deferreds):
        for _ in xrange(n - len(deferreds)):
            deferreds.append(self.waitForStranger())
        d = defer.DeferredList(deferreds)
        d.addCallback(self._ensureNStrangers, n)
        return d

    def _ensureNStrangers(self, results, n):
        strangers = [s for success, s in results if success and s.state == 'got-peer']
        if len(strangers) != n:
            return self._waitForNStrangers(n, [defer.succeed(s) for s in strangers])
        return strangers

class StrangerPoolResource(Resource):
    def __init__(self, pool):
        Resource.__init__(self)
        self.pool = pool

    def strangerElement(self, stranger):
        return tags.form(
            tags.h4(repr(stranger)),
            tags.dl(
                tags.dt('State'),
                tags.dd(tags.code(stranger.state)),
                tags.dt('Started at'),
                tags.dd(str(stranger.startedAt)),
                tags.dt('ID'),
                tags.dd(tags.code(str(stranger.clientID))),
                tags.dt('Interests'),
                tags.dd(tags.ul(*[tags.li(i) for i in stranger.commonLikes])
                        if stranger.commonLikes is not None else 'None'),
                tags.dt('Available servers'),
                tags.dd(tags.ul(*[tags.li(s) for s in stranger.servers])
                        if stranger.servers is not None else 'None'),
                tags.dt('Active requests'),
                tags.dd(tags.ul(*[tags.li(u) for u in stranger._requestDeferreds.itervalues()])
                        if stranger._requestDeferreds is not None else 'None')),
            tags.input(type='hidden', name='id', value=str(stranger.strangerID)),
            tags.button('Disconnect', name='action', value='disconnect'),
            action='', method='post',
        )

    def strangerGroupElement(self, groupName, group):
        return tags.li(
            groupName, '(%d)' % (len(group),),
            tags.ul(*[tags.li(self.strangerElement(s) for s in group)]))

    def render_GET(self, request):
        body = tags.ul(
            self.strangerGroupElement('Connecting', self.pool._connecting),
            self.strangerGroupElement('Ready', self.pool._ready),
            self.strangerGroupElement('Connected', self.pool._connected))
        return renderElement(request, body)

    def render_POST(self, request):
        action = request.args.get('action', [None])[0]
        strangerID = request.args.get('id', [None])[0]
        stranger = self.pool.strangers.get(strangerID)
        if stranger is None:
            return self.render_GET(request)
        if action == 'disconnect':
            d = stranger.disconnect()
            return renderElement(request, d.addCallback(str))
        return self.render_GET(request)

class RecaptchaSolverResource(Resource):
    def __init__(self, pool):
        Resource.__init__(self)
        self.pool = pool

    def recaptchaElement(self, stranger):
        return tags.form(
            tags.script(
                type='text/javascript',
                src='http://www.google.com/recaptcha/api/challenge?k=%s' % (stranger.recaptchaPublicKey,)),
            tags.input(type='hidden', name='id', value=str(stranger.strangerID)),
            tags.button('Solve recaptcha', name='action', value='recaptcha'),
            action='', method='post',
        )

    def render_GET(self, request, excluding=()):
        for s in self.pool._connecting:
            if s.state == 'needs-recaptcha' and s not in excluding:
                body = self.recaptchaElement(s)
                break
        else:
            body = tags.p("No strangers need to be recaptcha'd.")
        return renderElement(request, body)

    def render_POST(self, request):
        action = request.args.get('action', [None])[0]
        strangerID = request.args.get('id', [None])[0]
        stranger = self.pool.strangers.get(strangerID)
        if stranger is None:
            return self.render_GET(request)
        excluding = ()
        if action == 'recaptcha':
            d = stranger.solveRecaptcha(
                request.args['recaptcha_challenge_field'][0],
                request.args['recaptcha_response_field'][0])
            d.addErrback(log.err, '%s: error submitting captcha' % (stranger,))
            excluding = stranger,
        return self.render_GET(request, excluding)
