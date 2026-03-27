"""
Riverside Regional Jail — automated roster scraper
Runs via GitHub Actions, outputs data.json
"""
import requests, json, re, base64, time, os
from bs4 import BeautifulSoup

BASE     = 'http://66.217.205.242:8180/IML'
IMG_BASE = 'http://66.217.205.242:8180/imageservlet'
LETTERS  = list('ABCDEFGHIJKLMNOPQRSTUVWXYZ')

OCIS_BASE = 'https://eapps.courts.state.va.us'
OCIS_API  = OCIS_BASE + '/ocis-rest/api/public/'

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

def fetch_inmate_detail(sess, sysID, imgSysID):
    """Fetch detail page: sex, race, county, commitmentDate, charges, bonds."""
    try:
        r = sess.post(BASE, data={
            'flow_action': 'edit',
            'sysID':       sysID,
            'imgSysID':    imgSysID,
        }, timeout=30)
        if not r.ok:
            return {}
        html = r.text
        soup = BeautifulSoup(html, 'html.parser')

        # Extract label:value pairs from adjacent <td> elements
        tds = soup.find_all('td')
        def get_val(label):
            for i, td in enumerate(tds):
                if td.get_text(strip=True) == label and i + 1 < len(tds):
                    return tds[i + 1].get_text(strip=True)
            return ''

        sex             = get_val('Sex:')
        race            = get_val('Race:')
        county          = get_val('County:')
        commitment_date = get_val('Commitment Date:')
        location        = get_val('Current Location:')

        # Bond section (Bond Information appears before Charge Information in HTML)
        bi = html.find('Bond Information')
        ci = html.find('Charge Information')
        bond_soup = BeautifulSoup(html[bi:ci] if bi >= 0 and ci > bi else '', 'html.parser')
        bonds = []
        for row in bond_soup.find_all('tr', attrs={'bgcolor': lambda v: v and v.upper() == '#FFFFFF'}):
            cells = [td.get_text(strip=True) for td in row.find_all('td')]
            if len(cells) >= 2 and cells[1]:
                bonds.append({
                    'caseNum':  cells[0],
                    'bondType': cells[1],
                    'amount':   cells[2] if len(cells) > 2 else '',
                })

        # Charge section
        charge_soup = BeautifulSoup(html[ci:] if ci >= 0 else '', 'html.parser')
        charges = []
        for row in charge_soup.find_all('tr', attrs={'bgcolor': lambda v: v and v.upper() in ('#FFFFFF', '#CCCCFF')}):
            cells = [td.get_text(strip=True) for td in row.find_all('td')]
            if len(cells) >= 4 and any(cells[1:4]):
                charges.append({
                    'offenseDate': cells[1] if len(cells) > 1 else '',
                    'code':        cells[2] if len(cells) > 2 else '',
                    'description': cells[3] if len(cells) > 3 else '',
                    'grade':       cells[4] if len(cells) > 4 else '',
                })

        return {
            'sex':            sex,
            'race':           race,
            'county':         county,
            'commitmentDate': commitment_date,
            'location':       location,
            'charges':        charges,
            'bonds':          bonds,
        }
    except Exception as e:
        print(f'  Detail error sysID={sysID}: {e}')
        return {}


def init_ocis_session(sess):
    """Accept OCIS 2.0 T&C once to establish a valid session."""
    try:
        sess.get(OCIS_BASE + '/ocis/landing', timeout=15)
        sess.get(OCIS_API + 'termsAndCondAccepted', timeout=15)
    except Exception as e:
        print(f'  OCIS session init failed: {e}')

def fetch_va_court(sess, name, dob):
    """Search OCIS 2.0 statewide for adult criminal/traffic cases by name."""
    try:
        payload = {
            'courtLevels':    [],
            'divisions':      ['Adult Criminal/Traffic'],
            'selectedCourts': [],
            'searchString':   [name.strip()],
            'searchBy':       'N',
        }
        r = sess.post(
            OCIS_API + 'search',
            json=payload,
            timeout=20,
            headers={
                'Content-Type': 'application/json',
                'Referer': OCIS_BASE + '/ocis/search',
            }
        )
        if not r.ok:
            return []
        data = r.json()
        results = (data.get('context', {})
                       .get('entity', {})
                       .get('payload', {})
                       .get('searchResults', []))
        cases = []
        for row in results:
            cases.append({
                'formattedCaseNum': row.get('formattedCaseNumber', ''),
                'caseTrackingID':   row.get('caseNumber', ''),
                'court':            row.get('qualifiedFips', ''),
                'courtLevel':       row.get('courtLevel', ''),
                'offenseDate':      row.get('offenseDate', ''),
                'codeSection':      row.get('codeSection', ''),
                'chargeDesc':       row.get('chargeDesc', ''),
                'dispositionDate':  row.get('hearingDate', ''),
                'dispositionDesc':  '',
                'sentence':         '',
            })
        return cases
    except Exception as e:
        print(f'  OCIS error for {name}: {e}')
        return []

def main():
    sess = requests.Session()
    sess.headers['User-Agent'] = 'Mozilla/5.0'
    init_ocis_session(sess)

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

        # Fetch full detail page (sex, race, county, charges, bonds)
        detail = fetch_inmate_detail(sess, inmate['sysID'], inmate['imgSysID'])
        time.sleep(0.2)

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
            'sex':           detail.get('sex') or ex.get('sex', ''),
            'race':          detail.get('race') or ex.get('race', ''),
            'location':      detail.get('location') or ex.get('location', ''),
            'county':        detail.get('county') or ex.get('county', ''),
            'commitmentDate':detail.get('commitmentDate') or ex.get('commitmentDate', ''),
            'releaseDate':   inmate['releaseDate'],
            'charges':       detail.get('charges') or ex.get('charges', []),
            'bonds':         detail.get('bonds') or ex.get('bonds', []),
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
