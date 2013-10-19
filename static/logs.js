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

source.onmessage = function (event) {
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
    window.scrollTo(0, document.body.scrollHeight);
}
