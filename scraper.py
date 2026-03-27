"""
Riverside Regional Jail — automated roster scraper
Runs via GitHub Actions, outputs data.json
"""
import requests, json, re, base64, time, os
from bs4 import BeautifulSoup

BASE     = 'http://66.217.205.242:8180/IML'
IMG_BASE = 'http://66.217.205.242:8180/imageservlet'
LETTERS  = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ')

VA_COURT = 'https://eapps.courts.state.va.us/gdcourts/caseSearch.do'

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

def fetch_va_court(sess, name, dob):
    """Search Virginia court system by name+DOB. Returns list of case dicts."""
    try:
        # Parse "LAST, FIRST" format
        parts = name.split(',', 1)
        last  = parts[0].strip()
        first = parts[1].strip().split()[0] if len(parts) > 1 else ''
        r = sess.get(VA_COURT, params={
            'fromSidebar': 'true',
            'searchDivision': 'T',
            'searchLastName': last,
            'searchFirstName': first,
            'searchDOB': dob,
            'searchCDLNumber': '',
            'searchUCN': '',
            'searchCaseNumber': '',
            'searchFIPSCode': '',
            'searchNameSearch': 'true',
        }, timeout=20, headers={'Referer': VA_COURT})
        if not r.ok:
            return []
        soup = BeautifulSoup(r.text, 'html.parser')
        cases = []
        for row in soup.select('table.tableborder tr'):
            cells = [td.get_text(strip=True) for td in row.find_all('td')]
            if len(cells) < 6:
                continue
            # Columns: Case#, Name, DOB, Charge, Court, OffenseDate
            case_num = cells[0].strip()
            if not re.match(r'[A-Z]{2}\d+', case_num):
                continue
            cases.append({
                'formattedCaseNum': case_num,
                'caseTrackingID':   re.sub(r'\D', '', case_num),
                'court':            cells[4] if len(cells) > 4 else '',
                'courtLevel':       'G',
                'offenseDate':      cells[5] if len(cells) > 5 else '',
                'codeSection':      '',
                'chargeDesc':       cells[3] if len(cells) > 3 else '',
                'dispositionDate':  '',
                'dispositionDesc':  '',
                'sentence':         '',
            })
        return cases
    except Exception as e:
        return []

def main():
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0'

    # ── Load court_data.json (keyed by booking number) ───────────────────
    court_by_bn = {}
    if os.path.exists('court_data.json'):
        with open('court_data.json') as f:
            court_by_bn = json.load(f)

    # ── Load previous data.json ───────────────────────────────────────────
    prev = {}
    if os.path.exists('data.json'):
        with open('data.json') as f:
            for rec in json.load(f):
                prev[rec['bookingNum']] = rec

    # ── Build name+DOB secondary index for court lookup ───────────────────
    # Helps returning inmates who have a new booking number
    court_by_name_dob = {}
    for bn, cases in court_by_bn.items():
        p = prev.get(bn, {})
        n, d = p.get('name', ''), p.get('dob', '')
        if n and d and cases:
            court_by_name_dob[n.upper().strip() + '|' + d] = cases

    # ── Phase 1: scrape current roster ───────────────────────────────────
    roster = {}
    for letter in LETTERS:
        print(f'Scanning {letter}...')
        for inmate in search_letter(sess, letter):
            bn = inmate['bookingNum']
            if bn:
                roster[bn] = inmate
        time.sleep(0.4)
    print(f'Roster: {len(roster)} inmates')

    # ── Phase 2: fetch mugshots + court records ───────────────────────────
    records = []
    items = sorted(roster.items(), key=lambda x: x[0], reverse=True)
    new_court = dict(court_by_bn)  # will be updated with any new lookups

    for i, (bn, inmate) in enumerate(items):
        if i % 50 == 0:
            print(f'Processing {i}/{len(items)}...')
        ex = prev.get(bn, {})

        mugshot = fetch_mugshot(sess, inmate['sysID'], inmate['imgSysID'])
        if not mugshot:
            mugshot = ex.get('mugshot', '')

        # Court history: booking# → name+DOB fallback → VA court scrape
        name_dob_key = inmate['name'].upper().strip() + '|' + inmate['dob']
        court_hist = (
            court_by_bn.get(bn) or
            court_by_name_dob.get(name_dob_key) or
            ex.get('courtHistory', [])
        )
        if not court_hist:
            court_hist = fetch_va_court(sess, inmate['name'], inmate['dob'])
            if court_hist:
                new_court[bn] = court_hist
                print(f'  VA court: {inmate["name"]} → {len(court_hist)} cases')
            time.sleep(0.3)

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
            'courtHistory':  court_hist,
        })
        time.sleep(0.15)

    # Save updated court_data.json if we found new entries
    if new_court != court_by_bn:
        with open('court_data.json', 'w') as f:
            json.dump(new_court, f, separators=(',', ':'))
        print(f'Updated court_data.json ({len(new_court)} entries)')

    with open('data.json', 'w') as f:
        json.dump(records, f, separators=(',', ':'))
    print(f'Saved {len(records)} records to data.json')

if __name__ == '__main__':
    main()
