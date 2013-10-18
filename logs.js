var source = new EventSource(stream);
var content = document.getElementById('content');
source.onmessage = function (event) {
    content.appendChild(document.createTextNode(event.data + '\n'));
    window.scrollTo(0, document.body.scrollHeight);
}
