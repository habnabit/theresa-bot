# coding: utf-8

from __future__ import print_function

from twisted.internet import defer, task, endpoints
from twisted.internet.error import AlreadyCalled
from twisted.python import log
from twisted.web import client, server
from twisted.web.resource import Resource
from twisted.web.server import NOT_DONE_YET
from twisted.web.static import File
from twisted.web.template import tags, renderElement

from functools import partial
import collections
import datetime
import omoogle
import random
import json
import time
import yaml
import sys
import re
import os


def timeoutFunc(nCharacters):
    return 300 * nCharacters ** 0.2 + 10

def disconnectQuietly(stranger):
    try:
        stranger.disconnect()
    except omoogle.InvalidState:
        pass


class FileLikeResource(Resource):
    def __init__(self, fobj):
        Resource.__init__(self)
        self.fobj = fobj
        self._listeners = set()
        self._sessionfile = None
        self._context = collections.deque(maxlen=50)

    def render_GET(self, request):
        self._listeners.add(request)
        request.notifyFinish().addBoth(self._doneWith, request)
        request.setHeader('content-type', 'text/event-stream')
        for chunk in list(self._context):
            self.write(chunk, [request])
        return NOT_DONE_YET

    def _doneWith(self, result, request):
        self._listeners.discard(request)

    def write(self, data, listeners=None):
        if listeners is None:
            self.fobj.write(data)
            if not self._sessionfile:
                self.newSession()
            self._sessionfile.write(data)
            self._context.append(data)
            listeners = self._listeners

        if not listeners:
            return

        data = '\n'.join('data: ' + line for line in data.splitlines()) + '\n\n'
        for listener in listeners:
            listener.write(data)

    def flush(self):
        self.fobj.flush()
        if self._sessionfile is not None:
            self._sessionfile.flush()

    def newSession(self):
        if self._sessionfile is not None:
            self._sessionfile.close()
        sessionPath = 'sessions/%s.txt' % (datetime.datetime.now().isoformat(),)
        self._sessionfile = open(sessionPath, 'w')


class MiddleManager(object):
    def __init__(self, clock, pool, conversationCount):
        self.clock = clock
        self.pool = pool
        self.conversationCount = conversationCount
        self.strangerCapacity = self.conversationCount * 2
        self.wiring = {}
        self._logs = {}
        self._timeouts = {}
        self._logPool = [FileLikeResource(open('logs/%s.txt' % (i,), 'a'))
                         for i in xrange(conversationCount)]
        self.logResources = list(self._logPool)
        self._looper = task.LoopingCall(self._run)

    def _bumpTimeoutCounter(self, stranger, increment):
        d = self._timeouts[stranger]
        d['count'] += increment
        d['timeout'] = timeoutFunc(d['count'])
        d['canceler'].reset(d['timeout'])

    def _swapStrangers(self, s2, s3):
        "Imagine, if you will, you're swapping s2 and s3 in [[s1, s2], [s3, s4]]."
        self._reflectEvent(s2, ['swap', s3])
        self._reflectEvent(s3, ['swap', s2])
        s1 = self.wiring[s2]
        s4 = self.wiring[s3]
        for s, o in [[s1, s3], [s2, s4]]:
            self.wiring[s] = o
            self.wiring[o] = s
        self._logs[s2], self._logs[s3] = self._logs[s3], self._logs[s2]

    def _reflectEvent(self, stranger, event):
        if stranger not in self.wiring:
            return

        _, l = self._logs[stranger]
        otherStranger = self.wiring[stranger]
        if event[0] == 'connected':
            l('connected')
        elif event[0] == 'commonLikes':
            l('interests: %r' % (event[1],))
        elif event[0] == 'gotMessage':
            message = event[1]
            self._bumpTimeoutCounter(stranger, len(message))
            self._bumpTimeoutCounter(otherStranger, len(message))
            l(message, depth=2)
            otherStranger.sendMessage(message)
        elif event[0] == 'typing':
            self._bumpTimeoutCounter(stranger, 0)
            l('started typing')
            otherStranger.startedTyping()
        elif event[0] == 'stoppedTyping':
            l('stopped typing')
            otherStranger.stoppedTyping()
        elif event[0] == 'strangerDisconnected':
            l('fake disconnect')
        elif event[0] == 'strangerReallyDisconnected':
            l('disconnected')
            otherStranger.disconnect()
        elif event[0] == 'swap':
            l('swapping with a stranger with interests: %r' % (event[1].commonLikes,))
        elif event[0] == 'identDigests':
            l(event[1].replace(',', ' '))

    def _timeoutStranger(self, stranger):
        _, l = self._logs[stranger]
        l('timeout')
        disconnectQuietly(stranger)

    def _cleanupStranger(self, ign, s):
        other = self.wiring.pop(s)
        disconnectQuietly(other)
        t = self._timeouts.pop(s)
        try:
            t['canceler'].cancel()
        except AlreadyCalled:
            pass
        logfile, l = self._logs.pop(s)
        l('disconnected')
        if logfile not in self._logPool:
            self._logPool.append(logfile)

    def _openLog(self, s1, s2):
        logfile = self._logPool.pop(0)
        logfile.newSession()
        def logMessage(glyph, message, depth=1):
            logfile.write((u'[%s] %s %s\n' % (time.strftime('%T'), glyph * depth, message)).encode('utf-8'))
            logfile.flush()
        self._logs[s1] = logfile, partial(logMessage, '<')
        self._logs[s2] = logfile, partial(logMessage, '>')

    @defer.inlineCallbacks
    def _run(self):
        if len(self.wiring) >= self.strangerCapacity:
            return

        s1, s2 = yield self.pool.waitForNStrangers(2)
        log.msg('opening %s x %s' % (s1, s2))
        self._openLog(s1, s2)
        for s, o in [[s1, s2], [s2, s1]]:
            self.wiring[s] = o
            self._timeouts[s] = {
                'count': 0,
                'timeout': 10,
                'canceler': self.clock.callLater(10, self._timeoutStranger, s),
            }
            s.waitForState('done').addCallback(self._cleanupStranger, s)
        for s in [s1, s2]:
            self.clock.callLater(0.5, s.setEventCallback, partial(self._reflectEvent, s))

    def start(self, restartInterval=1):
        return self._looper.start(restartInterval)

    def stop(self):
        self._looper.stop()
        return defer.DeferredList([s.waitForState('done') for s in self.wiring])

    def voiceOfGod(self, stranger, message):
        _, logf = self._logs[stranger]
        other = self.wiring[stranger]
        _, otherLogf = self._logs[other]
        message = u'»ᴛʜᴇ ᴠᴏɪᴄᴇ ᴏғ ɢᴏᴅ ʙᴏᴏᴍs: ' + message
        logf(message, depth=2)
        otherLogf(message, depth=2)
        d = defer.gatherResults([
            stranger.sendMessage(message),
            other.sendMessage(message),
        ])
        return d

class MiddleManagerResource(Resource):
    def __init__(self, manager):
        Resource.__init__(self)
        self.manager = manager

    def strangerElement(self, stranger):
        timeoutData = self.manager._timeouts[stranger]
        return tags.li(
            tags.h4(repr(stranger)),
            tags.dl(
                tags.dt('Wired to'),
                tags.dd(repr(self.manager.wiring[stranger])),
                tags.dt('Timeout'),
                tags.dd('%(timeout)0.4gs' % timeoutData),
                tags.dt('Throughput'),
                tags.dd('%(count)dB' % timeoutData),
                tags.dt('Context'),
                tags.dd(tags.pre(''.join(self.manager._logs[stranger][0]._context)))),
            tags.form(
                tags.input(type='hidden', name='id', value=str(stranger.strangerID)),
                tags.textarea(name='message'),
                tags.button('Voice of God', name='action', value='god'),
                action='', method='post'))

    def render_GET(self, request):
        body = tags.ul(*[self.strangerElement(s) for s in self.manager.wiring])
        return renderElement(request, body)

    def render_POST(self, request):
        action = request.args.get('action', [None])[0]
        strangerID = request.args.get('id', [None])[0]
        stranger = self.manager.pool.strangers.get(strangerID)
        if stranger is None:
            return self.render_GET(request)
        if action == 'god':
            d = self.manager.voiceOfGod(stranger, request.args.get('message', ['hello'])[0])
            return renderElement(request, d.addCallback(str))
        return self.render_GET(request)

class AllLogViewerResource(Resource):
    def __init__(self, nStreams):
        Resource.__init__(self)
        self.nStreams = nStreams

    def render_GET(self, request):
        body = [
            tags.iframe(src='/logs/%d' % (e,), seamless='', style='width: 95%; height: 30em; margin: 0.5em;')
            for e in xrange(self.nStreams)
        ]
        return renderElement(request, body)

class LogViewerResource(Resource):
    def __init__(self, stream):
        Resource.__init__(self)
        self.stream = stream

    def render_GET(self, request):
        body = [
            tags.link(rel='stylesheet', type='text/css', href='/static/logs.css'),
            tags.div(
                tags.div(id='content'),
                tags.div(id='spacer'),
            ),
            tags.script('var stream = %s;' % (json.dumps(self.stream),), type='text/javascript'),
            tags.script(type='text/javascript', src='/static/logs.js'),
        ]
        request.setHeader('content-type', 'text/html; charset: utf-8')
        return renderElement(request, body)

def main(reactor, configfile):
    with open(configfile) as fobj:
        config = yaml.safe_load(fobj)
    conversations = config['conversations']
    fileLogger = log.FileLogObserver(sys.stderr)
    toDiscard = [
        '^(Starting|Stopping) factory <twisted.web.client._HTTP11ClientFactory instance at [0-9a-fA-FxX]+>$',
    ]
    discardRegexp = re.compile('(?:%s)' % ('|'.join(toDiscard),))
    def observer(ev):
        if ev.get('message') and discardRegexp.match(ev['message'][0]):
            return
        fileLogger.emit(ev)
    log.defaultObserver.stop()
    log.addObserver(observer)
    if 'http-proxy' in config:
        proxyEndpoint = endpoints.clientFromString(reactor, config['http-proxy'])
        agent = client.ProxyAgent(proxyEndpoint, reactor)
    else:
        agent = client.Agent(reactor)
    strangerPool = omoogle.StrangerPool(reactor, agent, conversations * 2, 6)
    def connectStranger(s, randid):
        likes = random.choice(config['likes'])
        return s.connect(likes, randid)
    strangerPool.connectStranger = connectStranger
    strangerPool.start()
    manager = MiddleManager(reactor, strangerPool, conversations)
    root = Resource()
    root.putChild('strangers', omoogle.StrangerPoolResource(strangerPool))
    root.putChild('recaptcha', omoogle.RecaptchaSolverResource(strangerPool))
    root.putChild('manager', MiddleManagerResource(manager))
    logs = AllLogViewerResource(conversations)
    root.putChild('logs', logs)
    for e, logResource in enumerate(manager.logResources):
        stream = '%d.stream' % e
        logs.putChild(str(e), LogViewerResource(stream))
        logs.putChild(stream, logResource)
    logs.putChild('sessions', File('sessions'))
    root.putChild('static', File(os.path.join(os.path.dirname(__file__), 'static')))
    site = server.Site(root)
    serverEndpoint = endpoints.serverFromString(reactor, config.get('http-endpoint', 'tcp:8808'))
    deferreds = [
        serverEndpoint.listen(site),
        manager.start(),
    ]
    reactor.addSystemEventTrigger('before', 'shutdown', strangerPool.stop)
    reactor.addSystemEventTrigger('before', 'shutdown', manager.stop)
    return defer.gatherResults(deferreds)

if __name__ == '__main__':
    task.react(main, sys.argv[1:])
