"""Merge a fresher titledb fork over the nightly artefact files.

The nightly artefact (TITLEDB_ARTEFACTS_URL) is built from blawar/titledb, which can go weeks
without updates. TITLEDB_OVERLAY_REPO (default borntohonk/titledb) is a fork that is refreshed
weekly and keeps the same file layout, so its data is merged in additively:

- versions.json / versions.txt: version history is unioned, the highest version wins.
- cnmts.json / cnmts-fixed.json: missing title ids and versions are added.
- titles.<region>.<lang>.json: titles from the fork's <region>.<lang>.json (keyed by nsuId)
  that the artefact doesn't know are added, and empty fields of known titles are filled in.
  New DLC ids also get the same fallback CNMT entry the artefact build creates.

Nothing is ever removed, so the result is a superset of the artefact. The merge is re-run
whenever the fork's commit, the artefact commit, or any merged file changes on disk (for
example when the artefact re-downloads a file), and is skipped otherwise.
"""

import copy
import json
import logging
import os

from app.constants import *
from app import titledb

logger = logging.getLogger('main')

OVERLAY_SOURCE_DIR = os.path.join(TITLEDB_DIR, 'overlay')
OVERLAY_STATE_FILE = os.path.join(TITLEDB_DIR, '.overlay.json')
OVERLAY_SOURCE_FILES = ['versions.json', 'versions.txt', 'cnmts.json']


def _raw_url(filename):
    return f'https://raw.githubusercontent.com/{TITLEDB_OVERLAY_REPO}/{TITLEDB_OVERLAY_BRANCH}/{filename}'


def _remote_head_sha():
    url = f'https://api.github.com/repos/{TITLEDB_OVERLAY_REPO}/commits/{TITLEDB_OVERLAY_BRANCH}'
    r = titledb._get_with_retry(url, headers={'Accept': 'application/vnd.github.sha'}, allow_redirects=True)
    r.raise_for_status()
    sha = r.text.strip()
    if len(sha) != 40:
        raise ValueError(f'Unexpected commit response from {url}')
    return sha


def _file_sig(path):
    try:
        st = os.stat(path)
        return f'{st.st_size}:{getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))}'
    except OSError:
        return 'missing'


def _load_json(path):
    with open(path, 'r', encoding='utf-8-sig') as fp:
        return json.load(fp)


def _write_json(path, data):
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    os.replace(tmp, path)


def _read_state():
    try:
        with open(OVERLAY_STATE_FILE, 'r', encoding='utf-8') as fp:
            return json.load(fp) or {}
    except Exception:
        return {}


def _base_commit():
    try:
        with open(os.path.join(TITLEDB_DIR, '.latest'), 'r', encoding='utf-8') as fp:
            return fp.read().strip()
    except OSError:
        return ''


def _download_sources(region_file):
    """Fetch the fork's files into OVERLAY_SOURCE_DIR (conditional, so unchanged files are skipped)."""
    os.makedirs(OVERLAY_SOURCE_DIR, exist_ok=True)
    for name in OVERLAY_SOURCE_FILES:
        dest = os.path.join(OVERLAY_SOURCE_DIR, name)
        if name.endswith('.json'):
            titledb._download_json_file(_raw_url(name), dest, conditional=titledb._is_valid_json_file(dest))
        else:
            r = titledb._get_with_retry(_raw_url(name), allow_redirects=True)
            r.raise_for_status()
            if not r.content.startswith(b'id|'):
                raise ValueError(f'Unexpected {name} content from {TITLEDB_OVERLAY_REPO}')
            tmp = f'{dest}.tmp'
            with open(tmp, 'wb') as fp:
                fp.write(r.content)
            os.replace(tmp, dest)
    # The region descriptions file (e.g. US.en.json) is already fetched by titledb.py from
    # TITLEDB_DESCRIPTIONS_BASE_URL, which points at the same fork.
    region_path = os.path.join(TITLEDB_DIR, region_file)
    if not titledb._is_valid_json_file(region_path):
        titledb._download_json_file(_raw_url(region_file), region_path)
    return region_path


def _merge_versions_json(base, extra):
    added = 0
    for tid, hist in (extra or {}).items():
        if not isinstance(hist, dict):
            continue
        cur = base.setdefault(tid, {})
        if not isinstance(cur, dict):
            continue
        for ver, date in hist.items():
            if ver not in cur:
                cur[ver] = date
                added += 1
    return added


def _parse_versions_txt(path):
    rows = {}
    with open(path, 'r', encoding='utf-8', errors='ignore') as fp:
        for line in fp.read().splitlines()[1:]:
            parts = line.split('|')
            if len(parts) < 3 or not parts[0].strip():
                continue
            rows[parts[0].strip().lower()] = [parts[0].strip(), parts[1].strip(), parts[2].strip()]
    return rows


def _ver_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _merge_versions_txt(base_path, extra_path):
    base = _parse_versions_txt(base_path)
    changed = 0
    for key, row in _parse_versions_txt(extra_path).items():
        cur = base.get(key)
        if cur is None:
            base[key] = row
            changed += 1
            continue
        if _ver_int(row[2]) > _ver_int(cur[2]):
            cur[2] = row[2]
            changed += 1
        if not cur[1].strip('0') and row[1].strip('0'):
            cur[1] = row[1]
    if changed:
        tmp = f'{base_path}.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fp:
            fp.write('id|rightsId|version\n')
            for key in sorted(base):
                fp.write('|'.join(base[key]) + '\n')
        os.replace(tmp, base_path)
    return changed


def _merge_cnmts(base, extra):
    added = 0
    for tid, versions in (extra or {}).items():
        if not isinstance(versions, dict):
            continue
        cur = base.setdefault(tid, {})
        if not isinstance(cur, dict):
            continue
        for ver, entry in versions.items():
            if ver not in cur:
                cur[ver] = entry
                added += 1
    return added


def _derive_base_title_id(app_id):
    # Same rule as the artefact build (luketanti/aerofoil region_titles.yml).
    app_id = str(app_id or '').strip().upper()
    if len(app_id) != 16:
        return None
    if app_id.endswith('000'):
        return app_id
    if app_id.endswith('800'):
        return f'{app_id[:-3]}000'
    try:
        return (hex(int(app_id[:-3], 16) - 1)[2:].rjust(len(app_id[:-3]), '0') + '000').upper()
    except ValueError:
        return None


def _merge_titles(titles, region_data, region, language):
    """Add unknown titles from a nsuId-keyed region file, fill empty fields of known ones."""
    added, filled, new_ids = 0, 0, []
    for entry in (region_data or {}).values():
        if not isinstance(entry, dict) or not entry.get('id'):
            continue
        ids = entry.get('ids') or [entry['id']]
        for tid in ids:
            tid = str(tid or '').strip().upper()
            if len(tid) != 16:
                continue
            cur = titles.get(tid)
            if cur is None:
                new = copy.deepcopy(entry)
                new.pop('ids', None)
                new['id'] = tid
                new.setdefault('language', language)
                new.setdefault('regions', [region])
                new.setdefault('rank', None)
                titles[tid] = new
                new_ids.append(tid)
                added += 1
                continue
            for field, value in entry.items():
                if field in ('ids', 'id') or value in (None, '', [], {}):
                    continue
                if cur.get(field) in (None, '', [], {}):
                    cur[field] = copy.deepcopy(value)
                    filled += 1
    return added, filled, new_ids


def _add_fallback_dlc_cnmts(cnmts, titles, new_ids):
    added = 0
    for tid in new_ids:
        if tid.endswith('000') or tid.endswith('800'):
            continue
        key = tid.lower()
        if key in cnmts:
            continue
        base_id = _derive_base_title_id(tid)
        if not base_id or base_id not in titles:
            continue
        cnmts[key] = {
            "0": {
                "contentEntries": [],
                "metaEntries": [],
                "otherApplicationId": base_id.lower(),
                "requiredApplicationVersion": 0,
                "requiredSystemVersion": None,
                "titleId": key,
                "titleType": 130,
                "version": "0",
            }
        }
        added += 1
    return added


def _merged_paths(region_titles_file):
    paths = [os.path.join(TITLEDB_DIR, f) for f in ('versions.json', 'versions.txt', 'cnmts.json', region_titles_file)]
    fixed = os.path.join(TITLEDB_DIR, 'cnmts-fixed.json')
    if os.path.isfile(fixed):
        paths.append(fixed)
    return paths


def apply_overlay(app_settings):
    if not TITLEDB_OVERLAY_REPO:
        return False

    region = str(app_settings['titles']['region']).strip()
    language = str(app_settings['titles']['language']).strip()
    region_titles_file = titledb.get_region_titles_file(app_settings)
    region_file = f'{region}.{language}.json'

    remote_sha = _remote_head_sha()
    state = _read_state()
    current_sigs = {os.path.basename(p): _file_sig(p) for p in _merged_paths(region_titles_file)}
    if (
        state.get('repo') == TITLEDB_OVERLAY_REPO
        and state.get('sha') == remote_sha
        and state.get('base_commit') == _base_commit()
        and state.get('region_titles_file') == region_titles_file
        and state.get('files') == current_sigs
    ):
        logger.info(f'TitleDB overlay already applied ({TITLEDB_OVERLAY_REPO} {remote_sha[:7]})')
        return False

    logger.info(f'Applying TitleDB overlay from {TITLEDB_OVERLAY_REPO} {remote_sha[:7]}...')
    region_path = _download_sources(region_file)

    # versions.json
    versions_path = os.path.join(TITLEDB_DIR, 'versions.json')
    versions = _load_json(versions_path)
    n_versions = _merge_versions_json(versions, _load_json(os.path.join(OVERLAY_SOURCE_DIR, 'versions.json')))
    if n_versions:
        _write_json(versions_path, versions)
    del versions

    # versions.txt
    n_versions_txt = _merge_versions_txt(
        os.path.join(TITLEDB_DIR, 'versions.txt'),
        os.path.join(OVERLAY_SOURCE_DIR, 'versions.txt'),
    )

    # titles.<region>.<lang>.json
    titles_path = os.path.join(TITLEDB_DIR, region_titles_file)
    titles = _load_json(titles_path)
    n_titles, n_filled, new_ids = _merge_titles(titles, _load_json(region_path), region, language)
    if n_titles or n_filled:
        _write_json(titles_path, titles)

    # cnmts.json / cnmts-fixed.json
    extra_cnmts = _load_json(os.path.join(OVERLAY_SOURCE_DIR, 'cnmts.json'))
    n_cnmts = n_fixed = 0
    cnmts_path = os.path.join(TITLEDB_DIR, 'cnmts.json')
    cnmts = _load_json(cnmts_path)
    n_cnmts = _merge_cnmts(cnmts, extra_cnmts)
    if n_cnmts:
        _write_json(cnmts_path, cnmts)
    del cnmts
    fixed_path = os.path.join(TITLEDB_DIR, 'cnmts-fixed.json')
    if os.path.isfile(fixed_path):
        fixed = _load_json(fixed_path)
        n_fixed = _merge_cnmts(fixed, extra_cnmts)
        n_fixed += _add_fallback_dlc_cnmts(fixed, titles, new_ids)
        if n_fixed:
            _write_json(fixed_path, fixed)
        del fixed
    del titles, extra_cnmts

    state = {
        'repo': TITLEDB_OVERLAY_REPO,
        'sha': remote_sha,
        'base_commit': _base_commit(),
        'region_titles_file': region_titles_file,
        'files': {os.path.basename(p): _file_sig(p) for p in _merged_paths(region_titles_file)},
        'last_result': {
            'versions_json_added': n_versions,
            'versions_txt_changed': n_versions_txt,
            'titles_added': n_titles,
            'title_fields_filled': n_filled,
            'cnmts_added': n_cnmts,
            'cnmts_fixed_added': n_fixed,
        },
    }
    with open(OVERLAY_STATE_FILE, 'w', encoding='utf-8') as fp:
        json.dump(state, fp, indent=2)
    logger.info(f'TitleDB overlay applied: {state["last_result"]}')
    return True
