from __future__ import print_function

from cStringIO import StringIO
import random
import urllib

from PIL import Image, ImageFont, ImageDraw
from lxml import html
import treq
from twisted.internet import defer, task


IMGUR_TOKEN = '98746295bc6b491'
font = ImageFont.truetype('Impact.ttf', 42)


@defer.inlineCallbacks
def fetchYoupornComment():
    for x in xrange(5):
        body = yield treq.get('http://www.youporn.com/random/video/').addCallback(treq.content)
        doc = html.fromstring(body)
        comments = doc.xpath('//p[@class="message"]/text()')
        if comments:
            break
    else:
        defer.returnValue('I got nothin')
    defer.returnValue(random.choice(comments))


def fetchStockImage(width, height):
    return treq.get('http://lorempixel.com/%d/%d/' % (width, height)).addCallback(treq.content)


def splitTextLines(text, width, font=font):
    lines = []
    current = None
    greatest_height = None
    for word in text.split():
        if current is None:
            new_text = word
        else:
            new_text = '%s %s' % (current, word)
        new_width, new_height = font.getsize(new_text)
        if greatest_height is None or new_height > greatest_height:
            greatest_height = new_height
        if new_width > width:
            lines.append(current)
            current = word
        else:
            current = new_text
    lines.append(current)
    return lines, greatest_height


def overlayYoupornComment(inputs):
    comment, image = inputs
    im = Image.open(StringIO(image))
    im_width, im_height = im.size
    comment_lines, height = splitTextLines(comment, im_width)
    draw = ImageDraw.Draw(im)
    y_start = im_height - 20 - len(comment_lines) * height * 1.2
    for e, line in enumerate(comment_lines):
        text_width, _ = font.getsize(line)
        y = y_start + e * height * 1.2
        x = (im_width - text_width) / 2
        for coeff in [1, 2]:
            for xoff, yoff in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                draw.text((x + xoff * coeff, y + yoff * coeff),
                          line, font=font, fill='black')
        draw.text((x, y), line, font=font, fill='white')
    outfile = StringIO()
    im.save(outfile, format='JPEG')
    im.save('test.jpg')
    return outfile.getvalue()


def postToImgur(data):
    data = urllib.urlencode({
        'image': data.encode('base64'),
        'type': 'base64',
        'name': 'upload.jpg',
    })
    return treq.post(
        'https://api.imgur.com/3/image.json',
        headers={
            'Authorization': 'Client-ID ' + IMGUR_TOKEN,
            'Content-Type': 'application/x-www-form-urlencoded',
        },
        data=data,
    ).addCallback(treq.json_content)


def main(reactor):
    return (
        defer.gatherResults([
            fetchYoupornComment(),
            fetchStockImage(500, 500),
        ])
        .addCallback(overlayYoupornComment)
        .addCallback(postToImgur)
        .addCallback(print))


if __name__ == '__main__':
    task.react(main)
