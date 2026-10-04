#!/usr/bin/env python3
"""Pull Fallerfly's long-form uploads + view counts from YouTube into videos.json.

Run by .github/workflows/update-videos.yml on a schedule. Sources, best first:
  1. YouTube Data API   (only if the YT_API_KEY secret is set; exact numbers)
  2. yt-dlp             (no key; full upload list with current views)
  3. the channel RSS    (no key; latest 15 uploads, exact publish dates)
Results are merged with the previous videos.json so a single failed source
never wipes the site.

usage: update_videos.py <path/to/videos.json>
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

CHANNEL_ID = 'UCA-OVXXVRktMJ5tAbRMQonw'
UPLOADS_LONG = 'UULF' + CHANNEL_ID[2:]   # uploads playlist minus Shorts and lives
UA = {'User-Agent': 'Mozilla/5.0 (fallerfly.com video sync)'}


def log(*a):
    print(*a, file=sys.stderr)


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------- sources ----------

def from_api(key):
    def call(path, **q):
        q['key'] = key
        return json.loads(get('https://www.googleapis.com/youtube/v3/' + path + '?' + urllib.parse.urlencode(q)))

    ch = call('channels', part='statistics', id=CHANNEL_ID)['items'][0]['statistics']
    ids, page = [], None
    while True:
        q = dict(part='contentDetails', playlistId=UPLOADS_LONG, maxResults=50)
        if page:
            q['pageToken'] = page
        res = call('playlistItems', **q)
        ids += [it['contentDetails']['videoId'] for it in res['items']]
        page = res.get('nextPageToken')
        if not page:
            break
    vids = []
    for i in range(0, len(ids), 50):
        for it in call('videos', part='snippet,statistics', id=','.join(ids[i:i + 50]))['items']:
            vids.append({
                'id': it['id'],
                'title': it['snippet']['title'],
                'views': int(it['statistics'].get('viewCount', 0)),
                'published': it['snippet']['publishedAt'],
                'exactDate': True,
            })
    channel = {
        'subscribers': None if ch.get('hiddenSubscriberCount') else int(ch['subscriberCount']),
        'videos': int(ch['videoCount']),
    }
    return channel, vids


def from_ytdlp():
    from yt_dlp import YoutubeDL
    opts = {
        'extract_flat': 'in_playlist', 'quiet': True, 'no_warnings': True,
        'skip_download': True, 'ignoreerrors': False,
        'extractor_args': {'youtubetab': {'approximate_date': ['']}},
    }
    base = 'https://www.youtube.com/channel/' + CHANNEL_ID
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(base + '/videos', download=False)
        vids = []
        for e in info.get('entries') or []:
            if not e or not e.get('id'):
                continue
            vids.append({
                'id': e['id'],
                'title': e.get('title') or '',
                'views': e.get('view_count'),
                'published': iso(e['timestamp']) if e.get('timestamp') else None,
                'exactDate': False,
            })
        total = len(vids)
        try:
            shorts = ydl.extract_info(base + '/shorts', download=False)
            total += len([e for e in shorts.get('entries') or [] if e])
        except Exception as ex:  # a channel with no Shorts tab is fine
            log('shorts count skipped:', ex)
    channel = {'subscribers': info.get('channel_follower_count'), 'videos': total}
    return channel, vids


def from_rss():
    ns = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015',
          'm': 'http://search.yahoo.com/mrss/'}
    root = ET.fromstring(get('https://www.youtube.com/feeds/videos.xml?playlist_id=' + UPLOADS_LONG))
    vids = []
    for en in root.findall('a:entry', ns):
        stats = en.find('m:group/m:community/m:statistics', ns)
        vids.append({
            'id': en.findtext('yt:videoId', namespaces=ns),
            'title': en.findtext('a:title', namespaces=ns) or '',
            'views': int(stats.get('views')) if stats is not None and stats.get('views') else None,
            'published': (en.findtext('a:published', namespaces=ns) or '')[:19] + 'Z',
            'exactDate': True,
        })
    return vids


# ---------- merge ----------

def merge(prev, primary, extra):
    """primary: full list (or None if no full source worked). extra: partial lists."""
    old = {v['id']: v for v in prev.get('videos', [])}
    if primary is not None:
        out = {v['id']: dict(v) for v in primary}
    else:
        out = {k: dict(v) for k, v in old.items()}
    for src in extra:
        for v in src:
            cur = out.setdefault(v['id'], dict(v))
            if v.get('title'):
                cur['title'] = v['title']
            if v.get('views') is not None:
                cur['views'] = max(cur.get('views') or 0, v['views'])
            if v.get('exactDate'):
                cur['published'], cur['exactDate'] = v['published'], True
    for k, cur in out.items():
        o = old.get(k)
        if not o:
            continue
        if cur.get('views') is None:
            cur['views'] = o.get('views')
        # approximate dates drift a little each run; keep the first/exact one
        if o.get('published') and (o.get('exactDate') or not cur.get('exactDate')):
            cur['published'], cur['exactDate'] = o['published'], o.get('exactDate', False)
    vids = sorted(out.values(), key=lambda v: v.get('published') or '', reverse=True)
    return vids


def main():
    path = sys.argv[1]
    try:
        with open(path) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}

    channel, primary, extra, used = None, None, [], []
    key = os.environ.get('YT_API_KEY', '').strip()
    if key:
        try:
            channel, primary = from_api(key)
            used.append('api')
        except Exception as ex:
            log('YouTube API failed:', ex)
    if primary is None:
        try:
            channel, primary = from_ytdlp()
            used.append('yt-dlp')
        except Exception as ex:
            log('yt-dlp failed:', ex)
    try:
        extra.append(from_rss())
        used.append('rss')
    except Exception as ex:
        log('RSS failed:', ex)

    if not used:
        log('every source failed; leaving videos.json untouched')
        sys.exit(1)

    pch = prev.get('channel', {})
    channel = channel or {}
    data = {
        'updated': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'sources': used,
        'channel': {
            'id': CHANNEL_ID,
            'subscribers': channel.get('subscribers') or pch.get('subscribers'),
            'videos': channel.get('videos') or pch.get('videos'),
        },
        'videos': merge(prev, primary, extra),
    }
    with open(path, 'w') as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write('\n')
    log('wrote %d videos via %s' % (len(data['videos']), '+'.join(used)))


if __name__ == '__main__':
    main()
