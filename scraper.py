"""
Riverside Regional Jail — automated roster scraper
Runs via GitHub Actions, outputs data.json
"""
import requests, json, re, base64, time, os
from bs4 import BeautifulSoup

BASE     = 'http://66.217.205.242:8180/IML'
IMG_BASE = 'http://66.217.205.242:8180/imageservlet'
LETTERS  = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ')

def search_letter(sess, letter):
    try:
        r = sess.post(BASE, data={
            'flow_action': 'searchbyname',
            'quantity': '999',
            'systemUser_lastName': letter,
            'systemUser_firstName': '',
            'systemUser_includereleasedinmate': 'Y',
            'systemUser_includereleasedinmate2': 'Y',
            'searchtype': 'name'
        }, timeout=30)
        soup = BeautifulSoup(r.text, 'html.parser')
        result = []
        for tr in soup.find_all('tr', onclick=True):
            m = re.search(r"rowClicked\('(\d+)','(\d+)','(\d+)'\)", tr['onclick'])
            if not m: continue
            cells = [td.get_text(strip=True) for td in tr.find_all('td')]
            if len(cells) < 4: continue
            result.append({
                'sysID':      m.group(2),
                'imgSysID':   m.group(3),
                'name':       cells[0],
                'bookingNum': cells[1],
                'dob':        cells[3],
                'releaseDate':cells[4] if len(cells) > 4 else ''
            })
        return result
    except Exception as e:
        print(f'  Letter {letter} error: {e}')
        return []

def fetch_mugshot(sess, sysID, imgSysID):
    try:
        r = sess.get(f'{IMG_BASE}?sysid={sysID}&imgsysid={imgSysID}', timeout=15)
        if r.ok and len(r.content) > 500:
            mime = 'image/png' if r.content[:4] == b'\x89PNG' else 'image/jpeg'
            return f'data:{mime};base64,' + base64.b64encode(r.content).decode()
    except:
        pass
    return ''

def main():
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0'

    # Load court history (committed to repo, rarely changes)
    court = {}
    if os.path.exists('court_data.json'):
        with open('court_data.json') as f:
            court = json.load(f)

    # Load previous data.json to preserve detail fields for existing inmates
    prev = {}
    if os.path.exists('data.json'):
        with open('data.json') as f:
            for rec in json.load(f):
                prev[rec['bookingNum']] = rec

    # ── Phase 1: scrape current roster ──────────────────────────────────────
    roster = {}
    for letter in LETTERS:
        print(f'Scanning {letter}...')
        for inmate in search_letter(sess, letter):
            bn = inmate['bookingNum']
            if bn:
                roster[bn] = inmate
        time.sleep(0.4)
    print(f'Roster: {len(roster)} inmates')

    # ── Phase 2: fetch mugshots ──────────────────────────────────────────────
    records = []
    items = sorted(roster.items(), key=lambda x: x[0], reverse=True)
    for i, (bn, inmate) in enumerate(items):
        if i % 50 == 0:
            print(f'Mugshots {i}/{len(items)}...')
        ex = prev.get(bn, {})

        mugshot = fetch_mugshot(sess, inmate['sysID'], inmate['imgSysID'])
        if not mugshot:
            mugshot = ex.get('mugshot', '')  # fall back to cached

        records.append({
            'bookingNum':    bn,
            'name':          inmate['name'],
            'dob':           inmate['dob'],
            'sex':           ex.get('sex', ''),
            'race':          ex.get('race', ''),
            'location':      ex.get('location', ''),
            'county':        ex.get('county', ''),
            'commitmentDate':ex.get('commitmentDate', ''),
            'releaseDate':   inmate['releaseDate'],
            'charges':       ex.get('charges', []),
            'bonds':         ex.get('bonds', []),
            'mugshot':       mugshot,
            'courtHistory':  court.get(bn, ex.get('courtHistory', []))
        })
        time.sleep(0.15)

    with open('data.json', 'w') as f:
        json.dump(records, f, separators=(',', ':'))
    print(f'Saved {len(records)} records to data.json')

if __name__ == '__main__':
    main()
