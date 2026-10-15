#!/usr/bin/env python3
"""Build now.json for the Now page: what you're reading, watching, and listening to.

Sources
  Reading   Hardcover  ("Currently Reading" shelf)         needs HARDCOVER_TOKEN
  Watching  MyAnimeList ("Watching" list, top by score)    needs MAL_CLIENT_ID (+ username in now-config.json)
  Listening Last.fm    (top artists over a recent period)  needs LASTFM_API_KEY (+ username in now-config.json)

Each item carries a link (Hardcover book page, MAL anime page, Last.fm artist page), and now.json also holds a
"links" block with your profile on each site (used to link the Reading / Watching / Listening labels).

Keys are read from environment variables, never from a file. A source with no key or username is skipped.
If a source fails, its previous values in now.json are kept and the problem is recorded under "errors".
Standard library only.

  python3 now_sync.py            # writes now.json next to this script
  python3 now_sync.py --print    # prints instead of writing
"""
import argparse, datetime, json, os, sys, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = 'mnchrm-now-sync/1.0'


def http(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=dict({'User-Agent': UA}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        raise RuntimeError('HTTP %s from %s' % (e.code, urllib.parse.urlsplit(url).netloc))
    except urllib.error.URLError as e:
        raise RuntimeError('network error: %s' % e.reason)


def hardcover(cfg, token):
    limit = int(cfg.get('limit', 4))
    token = token.strip()
    if not token.lower().startswith('bearer '):
        token = 'Bearer ' + token
    url = os.environ.get('HARDCOVER_URL', 'https://api.hardcover.app/v1/graphql')
    headers = {'authorization': token, 'content-type': 'application/json'}

    def ask(query):
        out = http(url, headers, json.dumps({'query': query}).encode('utf-8'))
        if out.get('errors'):
            raise RuntimeError('Hardcover: ' + str(out['errors'][0].get('message', 'error')))
        me = out['data']['me']
        return me[0] if isinstance(me, list) else me      # the API returns a list with one user

    me = ask('query { me { user_books(where: {status_id: {_eq: 2}}, order_by: {updated_at: desc}, limit: %d) '
             '{ book { title slug contributions { author { name } } } } } }' % limit)
    items = []
    for ub in me['user_books']:
        b = ub['book']
        authors = [c['author']['name'] for c in (b.get('contributions') or []) if c.get('author') and c['author'].get('name')]
        item = {'title': b['title']}
        if authors:
            item['by'] = authors[0]
        if b.get('slug'):
            item['url'] = 'https://hardcover.app/books/' + urllib.parse.quote(b['slug'])
        items.append(item)

    profile = (cfg.get('profile_url') or '').strip()
    if not profile:                                       # separate query so a hiccup here never loses the book list
        try:
            name = (cfg.get('username') or '').strip() or ask('query { me { username } }').get('username')
            if name:
                profile = 'https://hardcover.app/@' + urllib.parse.quote(name)
        except Exception:
            pass
    return items, profile


def mal(cfg, client_id):
    user = (cfg.get('username') or '').strip()
    if not user:
        raise RuntimeError('MyAnimeList username missing in now-config.json')
    base = os.environ.get('MAL_BASE', 'https://api.myanimelist.net/v2')
    url = '%s/users/%s/animelist?%s' % (base, urllib.parse.quote(user), urllib.parse.urlencode(
        {'status': 'watching', 'limit': 100, 'fields': 'list_status', 'nsfw': 'true'}))
    out = http(url, {'X-MAL-CLIENT-ID': client_id})
    rows = out.get('data', [])
    rows.sort(key=lambda r: (-(r.get('list_status', {}).get('score') or 0), _neg(r.get('list_status', {}).get('updated_at'))))
    items = [{'title': r['node']['title'], 'url': 'https://myanimelist.net/anime/%s' % r['node']['id']}
             for r in rows[:int(cfg.get('limit', 4))]]
    return items, 'https://myanimelist.net/profile/' + urllib.parse.quote(user)


def _neg(iso):                                           # newest first as the tie-break
    return -datetime.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() if iso else 0


def lastfm(cfg, key):
    user = (cfg.get('username') or '').strip()
    if not user:
        raise RuntimeError('Last.fm username missing in now-config.json')
    url = os.environ.get('LASTFM_URL', 'https://ws.audioscrobbler.com/2.0/') + '?' + urllib.parse.urlencode(
        {'method': 'user.gettopartists', 'user': user, 'api_key': key, 'period': cfg.get('period', '7day'),
         'limit': int(cfg.get('limit', 4)), 'format': 'json'})
    out = http(url)
    if 'error' in out:
        raise RuntimeError('Last.fm: ' + str(out.get('message', out['error'])))
    items = []
    for a in out['topartists']['artist']:
        item = {'title': a['name']}
        if a.get('url'):
            item['url'] = a['url']
        items.append(item)
    return items, 'https://www.last.fm/user/' + urllib.parse.quote(user)


SOURCES = [('reading', 'hardcover', 'HARDCOVER_TOKEN', hardcover),
           ('watching', 'mal', 'MAL_CLIENT_ID', mal),
           ('listening', 'lastfm', 'LASTFM_API_KEY', lastfm)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--print', action='store_true')
    ap.add_argument('--out', default=os.path.join(HERE, 'now.json'))
    a = ap.parse_args()

    try:
        config = json.load(open(os.path.join(HERE, 'now-config.json'), encoding='utf-8'))
    except FileNotFoundError:
        config = {}
    try:
        old = json.load(open(a.out, encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        old = {}

    new = {k: old[k] for k in ('reading', 'watching', 'listening') if k in old}
    links = dict(old.get('links') or {})
    errors = {}
    for field, section, env, fn in SOURCES:
        secret = os.environ.get(env, '').strip()
        if not secret:
            errors[field] = '%s is not set; skipped' % env
            continue
        try:
            raw, profile = fn(config.get(section, {}), secret)
            items = [x for x in raw if x.get('title', '').strip()]
            if profile:
                links[field] = profile
            if items:
                new[field] = items
            else:
                new.pop(field, None)                      # nothing on the shelf: the page keeps your hand-written text
        except Exception as e:                            # keep the previous values, note the problem
            errors[field] = str(e)

    result = dict(new)
    if links:
        result['links'] = links
    if errors:
        result['errors'] = errors
    unchanged = {k: v for k, v in old.items() if k != 'updated'} == result
    if not unchanged:
        result['updated'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    else:
        result = old
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if a.print:
        print(text)
    elif not unchanged or not os.path.exists(a.out):
        open(a.out, 'w', encoding='utf-8').write(text)
    for f, msg in errors.items():
        print('NOTE %s: %s' % (f, msg), file=sys.stderr)


if __name__ == '__main__':
    main()
