var source = new EventSource(stream);
var content = document.getElementById('content');

function parseMessage(s) {
    data = {
        timestamp: s.slice(1, 9),
        direction: s[11] === '<'? 'left' : 'right',
        isText: s[12] !== ' ',
        message: s.slice(s[12] === ' '? 13 : 14),
    };
    return data;
}

var lastDirection = undefined;
var lastBox = undefined;
var scrollData = {
    startedAt: undefined,
    start: undefined,
    width: undefined,
    interval: undefined,
};

function now() {
    return (new Date()).getTime();
}

function scrollALittle() {
    var delta = (now() - scrollData.startedAt) / 200;
    if (delta > 1) {
        clearInterval(scrollData.interval);
        var coeff = 1;
    } else {
        var coeff = (-Math.cos(Math.PI * delta) + 1) / 2;
    }
    window.scrollTo(0, scrollData.start + scrollData.width * coeff);
}

function scrollToPosition(pos) {
    scrollData.startedAt = now();
    scrollData.start = pageYOffset;
    scrollData.width = pos - pageYOffset;
    clearInterval(scrollData.interval);
    scrollData.interval = setInterval(scrollALittle, 10);
}

source.onmessage = function (event) {
    var doScroll = document.body.scrollHeight - innerHeight - pageYOffset <= 64;
    data = parseMessage(event.data);
    if (data.direction !== lastDirection) {
        lastBox = document.createElement('div');
        lastBox.className = data.direction;
        lastDirection = data.direction;
        content.appendChild(lastBox);
    }
    var newNode = document.createElement('p');
    newNode.textContent = data.message;
    newNode.className = data.isText? 'message' : 'system';
    lastBox.appendChild(newNode);
    if (doScroll)
        scrollToPosition(document.body.scrollHeight - innerHeight);
}
